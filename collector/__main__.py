"""COMPAS Mission Control — data collector.

Reads repos.yml + features.yml, gathers data from GitHub and package registries, and
writes site/public/data.json (plus an optional dated snapshot in data-history/).
With ``--materials``, performs a lightweight metadata-only pass over materials.yml.

Usage:
    python -m collector --root .. --token $GITHUB_TOKEN
    GITHUB_TOKEN=... python collector           # token from env

Every field degrades to null/unknown on error; one bad repo never fails the run.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml

# Allow running as `python -m collector` or `python collector/__main__.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conda  # noqa: E402
import localrepo  # noqa: E402
import parse  # noqa: E402
import registries  # noqa: E402
from features import detect  # noqa: E402
from github import GitHub  # noqa: E402

WORKFLOW_CANDIDATES = ["build.yml", "test.yml", "ci.yml", "build_and_test.yml", "main.yml"]

# Dependency-stack tier for the ecosystem diagram, defaulted from category and
# overridable per repo via `tier` in repos.yml. Bottom → top of the stack.
TIER_BY_CATEGORY = {
    "core": "core",
    "geometry": "foundation",
    "fabrication": "domain",
    "timber": "domain",
    "structures": "domain",
    "fea": "domain",
    "xr": "domain",
    "other": "domain",
    "viz": "visualizers",
    "ai": "apps",
    "apps": "apps",
    "tooling": "tooling",
    "template": "tooling",
}

HISTORY_SCHEMA_VERSION = 2


def staleness(last_commit_date: str | None) -> str:
    if not last_commit_date:
        return "unknown"
    dt = datetime.date.fromisoformat(last_commit_date[:10])
    days = (datetime.date.today() - dt).days
    if days < 30:
        return "fresh"
    if days < 120:
        return "aging"
    if days < 365:
        return "stale"
    return "dormant"


def configured_distributions(cfg: dict) -> list[dict]:
    """Return normalized distribution config, including legacy ``pypi``."""
    configured = [d for d in (cfg.get("distributions") or []) if isinstance(d, dict)]
    result = []
    seen = set()
    if cfg.get("pypi"):
        result.append({"registry": "pypi", "name": cfg["pypi"]})
        seen.add(("pypi", cfg["pypi"]))
    for item in configured:
        kind, package_name = item.get("registry"), item.get("name")
        if kind and package_name and (kind, package_name) not in seen:
            result.append({"registry": kind, "name": package_name})
            seen.add((kind, package_name))
    return result


def collect_distributions(cfg: dict) -> list[dict]:
    """Collect current versions for every configured package registry."""
    result = []
    for item in configured_distributions(cfg):
        info = registries.latest(item["registry"], item["name"]) or {}
        result.append({
            **item,
            "version": info.get("version"),
            "date": info.get("date"),
            "url": registries.package_url(item["registry"], item["name"]),
        })
    return result


def resolve_ecosystem_deps(requirements: dict, cfg: dict, tracked: dict, self_ids: set[str]) -> list[str]:
    """Combine manifest-discovered and explicitly configured dependency edges."""
    dependencies = set(cfg.get("ecosystem_deps") or [])
    for dep_name in requirements:
        canon = parse.canonical_name(dep_name)
        target = tracked.get(canon)
        if target and canon not in self_ids and target != cfg["name"]:
            dependencies.add(target)
    return sorted(d for d in dependencies if d != cfg["name"])


def merge_repos(existing: list[dict], collected: list[dict]) -> list[dict]:
    """Replace freshly collected repos while preserving uncollected entries."""
    merged = {repo["name"]: repo for repo in existing}
    merged.update({repo["name"]: repo for repo in collected})
    return list(merged.values())


def build_history_snapshot(data: dict) -> dict:
    """Build a compact, versioned daily snapshot from fully collected data."""
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "date": data["generated_at"][:10],
        # Preserve the definition active on this date so later readers can
        # distinguish a newly introduced feature from non-adoption.
        "features": data.get("features", []),
        "repos": {
            repo["name"]: {
                "staleness": repo["health"]["staleness"],
                "ci": repo["health"]["ci"],
                "open_issues": repo["health"]["open_issues"],
                "open_prs": repo["health"]["open_prs"],
                "compas_major_floor": repo["packaging"]["compas_major_floor"],
                "features_adopted": sum(1 for cell in repo["features"].values() if cell["status"] == "adopted"),
                "features": {feature_id: cell["status"] for feature_id, cell in repo["features"].items()},
            }
            for repo in data["repos"]
        },
    }


def collect_material(gh: GitHub, cfg: dict, defaults: dict) -> dict:
    """Collect one lightweight workshop/project/reference record."""
    owner = cfg.get("owner", defaults.get("owner", "compas-dev"))
    name = cfg["name"]
    print(f"  · {owner}/{name}", file=sys.stderr)
    meta = gh.repo(owner, name)
    if not meta:
        gh.warnings.append(f"{owner}/{name}: repository metadata unavailable")
        meta = {}
    archived = meta.get("archived", False)
    status = cfg.get("status") or ("unknown" if not meta else "archived" if archived else "active")
    return {
        "name": name,
        "owner": owner,
        "url": meta.get("html_url", f"https://github.com/{owner}/{name}"),
        "kind": cfg.get("kind", "project"),
        "category": cfg.get("category", "other"),
        "status": status,
        "description": meta.get("description"),
        "language": meta.get("language") or None,
        "stars": meta.get("stargazers_count", 0),
        "last_activity_date": (meta.get("pushed_at") or "")[:10] or None,
        "homepage": meta.get("homepage") or None,
        "topics": meta.get("topics") or [],
        "ecosystem_deps": sorted(set(cfg.get("ecosystem_deps") or [])),
        "notes": cfg.get("notes"),
    }


def collect_repo(gh: GitHub, cfg: dict, defaults: dict, features: list[dict], tracked: dict,
                 scanner=None, counts: dict | None = None) -> dict:
    """Collect one repository.

    ``scanner`` optionally supplies file/tree content from a local checkout; it
    is API-compatible with ``gh`` for those reads and falls back to it for
    everything else. When omitted, every read goes through the GitHub API.

    ``counts`` optionally supplies pre-fetched open issue/PR counts from the
    batched GraphQL query; when omitted they are fetched per repo over REST.
    """
    owner = cfg.get("owner", defaults.get("owner", "compas-dev"))
    name = cfg["name"]
    branch = cfg.get("branch", defaults.get("branch", "main"))
    print(f"  · {owner}/{name}", file=sys.stderr)

    meta = gh.repo(owner, name) or {}
    branch = meta.get("default_branch") or branch
    # Metadata, CI, releases and issue counts have no local equivalent and stay
    # on the API; everything that reads repository content prefers the scanner.
    content = scanner if scanner is not None else gh
    runtime = cfg.get("runtime", "python")
    archived = meta.get("archived", False)
    status = cfg.get("status") or ("archived" if archived else "active")

    # -- health -----------------------------------------------------------
    if counts is None:
        counts = gh.issue_pr_counts(owner, name)
    last_commit = (meta.get("pushed_at") or "")[:10] or None
    health = {
        "last_commit_date": last_commit,
        "staleness": "dormant" if archived else staleness(last_commit),
        "ci": "none" if archived else gh.ci_status(owner, name, branch),
        "open_issues": counts["open_issues"],
        "open_prs": counts["open_prs"],
        "oldest_open_issue_age_days": counts["oldest_open_issue_age_days"],
    }

    # -- release / package registries -------------------------------------
    rel = gh.latest_release(owner, name) or {}
    gh_release_tag = rel.get("tag_name") or None
    gh_tag = parse.normalize_release_tag(gh_release_tag, cfg.get("release_tag_prefix"))
    gh_date = (rel.get("published_at") or "")[:10] or None
    distributions = collect_distributions(cfg)
    primary_distribution = distributions[0] if distributions else {}
    pypi_distribution = next((d for d in distributions if d["registry"] == "pypi"), {})
    comparable_versions = [d["version"] for d in distributions if d.get("version")]
    release = {
        "github_tag": gh_tag,
        "github_release_tag": gh_release_tag,
        "github_date": gh_date,
        "registry_version": primary_distribution.get("version"),
        "registry_date": primary_distribution.get("date"),
        "registry_name": primary_distribution.get("registry"),
        # Kept for consumers of the original data contract.
        "pypi_version": pypi_distribution.get("version"),
        "pypi_date": pypi_distribution.get("date"),
        "drift": bool(gh_tag and comparable_versions and any(not parse.same_version(gh_tag, version) for version in comparable_versions)),
        "distributions": distributions,
    }

    # -- packaging --------------------------------------------------------
    pyproject_text = content.file_text(owner, name, "pyproject.toml", branch) if runtime == "python" else None
    req_text = content.file_text(owner, name, "requirements.txt", branch) if runtime == "python" else None
    env_text = content.file_text(owner, name, "environment.yml", branch) if runtime == "python" else None
    package_text = content.file_text(owner, name, "package.json", branch) if runtime == "node" else None
    pyproject = parse.parse_pyproject(pyproject_text)
    package_json = parse.parse_package_json(package_text)
    # Merge dependency sources by precedence (later wins): conda environment.yml
    # < pyproject [project.dependencies] < requirements.txt. Covers `dynamic`
    # deps, static pyproject deps, and conda-only repos (e.g. compas_cra).
    requirements = {
        **parse.parse_environment_yml(env_text),
        **pyproject.get("dependencies", {}),
        **parse.parse_requirements(req_text),
        **package_json.get("dependencies", {}),
    }

    workflow_names = content.dir_entries(owner, name, ".github/workflows", branch)
    workflow_texts = []
    for wf in workflow_names:
        if wf in WORKFLOW_CANDIDATES or "build" in wf or "test" in wf or "ci" in wf:
            workflow_texts.append(content.file_text(owner, name, f".github/workflows/{wf}", branch) or "")
    ci_pythons = parse.parse_ci_pythons(workflow_texts) if runtime == "python" else []
    resolved = parse.resolve_pythons(ci_pythons, pyproject["classifier_pythons"], pyproject["requires_python"])

    compas_pin = requirements.get("compas")
    packaging = {
        "compas_pin": (f"compas {compas_pin}" if compas_pin else None),
        "compas_major_floor": parse.dependency_floor_major(compas_pin) if compas_pin else None,
        "python_versions": resolved["versions"],
        "python_source": resolved["source"],
        "hosts": parse.detect_hosts(content, owner, name, branch, workflow_names) if runtime == "python" else None,
        "node_engine": package_json.get("node_engine"),
        "package_manager": package_json.get("package_manager"),
        "requirements": requirements,  # used by feature engine; stripped before output
    }

    # -- features ---------------------------------------------------------
    feat_cells = {}
    for feature in features:
        feat_cells[feature["id"]] = detect(feature, cfg, packaging, content, owner, name, release=release, conda=conda)

    # -- ecosystem dependency edges (which tracked packages this depends on) --
    self_ids = {parse.canonical_name(name)}
    if cfg.get("pypi"):
        self_ids.add(parse.canonical_name(cfg["pypi"]))
    for distribution in configured_distributions(cfg):
        self_ids.add(parse.canonical_name(distribution["name"]))
    eco_deps = resolve_ecosystem_deps(packaging.get("requirements", {}), cfg, tracked, self_ids)

    packaging.pop("requirements", None)  # internal only
    category = cfg.get("category", "other")

    return {
        "name": name,
        "owner": owner,
        "url": meta.get("html_url", f"https://github.com/{owner}/{name}"),
        "category": category,
        "tier": cfg.get("tier") or TIER_BY_CATEGORY.get(category, "domain"),
        "runtime": runtime,
        "pypi": cfg.get("pypi"),
        "distributions": distributions,
        "role": cfg.get("role"),
        "status": status,
        "description": meta.get("description"),
        "stars": meta.get("stargazers_count", 0),
        "language": (meta.get("language") or None),
        "health": health,
        "release": release,
        "packaging": packaging,
        "features": feat_cells,
        "ecosystem_deps": eco_deps,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Collect COMPAS ecosystem data.")
    ap.add_argument("--root", default=".", help="repo root containing repos.yml / features.yml")
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub token")
    ap.add_argument("--no-history", action="store_true", help="skip writing a dated snapshot")
    ap.add_argument(
        "--materials",
        action="store_true",
        help="collect lightweight materials.yml metadata into site/public/materials.json",
    )
    ap.add_argument(
        "--local-scan",
        action="store_true",
        help="fetch each repo's source tarball and answer content/code checks locally",
    )
    ap.add_argument(
        "--scan-dir",
        help="reuse/keep extracted trees here instead of a temp dir (implies --local-scan)",
    )
    ap.add_argument(
        "--scan-workers",
        type=int,
        default=8,
        help="parallel tarball fetches for --local-scan (default 8)",
    )
    ap.add_argument(
        "--repo",
        dest="repo_names",
        action="append",
        help="collect only this repo and merge it into the existing data.json (repeatable)",
    )
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if args.materials:
        if args.repo_names:
            print("ERROR: --repo cannot be combined with --materials", file=sys.stderr)
            return 2
        materials_cfg = yaml.safe_load((root / "materials.yml").read_text())
        defaults = materials_cfg.get("defaults", {})
        gh = GitHub(args.token)
        configs = materials_cfg.get("materials", [])
        print(f"Collecting all {len(configs)} materials…", file=sys.stderr)
        materials = [collect_material(gh, cfg, defaults) for cfg in configs]
        materials.sort(key=lambda item: (item["owner"].lower(), item["name"].lower()))
        materials.sort(key=lambda item: item["last_activity_date"] or "", reverse=True)
        data = {
            "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "materials": materials,
            "warnings": gh.warnings,
        }
        out = root / "site" / "public" / "materials.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=2))
        print(f"Wrote {out} ({len(materials)} materials, {len(gh.warnings)} warnings)", file=sys.stderr)
        return 0

    repos_cfg = yaml.safe_load((root / "repos.yml").read_text())
    features = yaml.safe_load((root / "features.yml").read_text())["features"]
    defaults = repos_cfg.get("defaults", {})
    requested_names = set(args.repo_names or [])
    known_names = {cfg["name"] for cfg in repos_cfg["repos"]}
    unknown_names = requested_names - known_names
    if unknown_names:
        print(f"ERROR: unknown repo(s): {', '.join(sorted(unknown_names))}", file=sys.stderr)
        return 2
    selected_cfgs = [cfg for cfg in repos_cfg["repos"] if not requested_names or cfg["name"] in requested_names]

    if not args.token:
        print("WARNING: no GitHub token — unauthenticated rate limits are very low.", file=sys.stderr)

    # Map every tracked package identifier (repo name + pypi name) to its repo
    # name, so we can resolve inter-ecosystem dependency edges.
    tracked: dict[str, str] = {}
    for cfg in repos_cfg["repos"]:
        tracked[parse.canonical_name(cfg["name"])] = cfg["name"]
        for distribution in configured_distributions(cfg):
            tracked[parse.canonical_name(distribution["name"])] = cfg["name"]

    gh = GitHub(args.token)
    scope = f"{len(selected_cfgs)} selected" if requested_names else f"all {len(selected_cfgs)}"

    # -- optional local scan ------------------------------------------------
    local_scan = args.local_scan or bool(args.scan_dir)
    trees: dict = {}
    temp_scan_dir = None
    if local_scan:
        if args.scan_dir:
            scan_root = Path(args.scan_dir).resolve()
        else:
            temp_scan_dir = tempfile.mkdtemp(prefix="mc-scan-")
            scan_root = Path(temp_scan_dir)
        entries = [
            (cfg.get("owner", defaults.get("owner", "compas-dev")), cfg["name"], None)
            for cfg in selected_cfgs
        ]
        print(f"Fetching {len(entries)} source trees into {scan_root}…", file=sys.stderr)
        started = time.time()
        trees = localrepo.prefetch(
            entries, args.token, scan_root, workers=args.scan_workers,
            log=lambda message: print(message, file=sys.stderr),
        )
        print(f"  fetched {len(trees)}/{len(entries)} trees in {time.time() - started:.0f}s", file=sys.stderr)
        missing = [f"{owner}/{name}" for owner, name, _ in entries if (owner, name) not in trees]
        for ref in missing:
            gh.warnings.append(f"{ref}: local scan unavailable, fell back to the GitHub API")

    # -- batched issue/PR counts -------------------------------------------
    # One GraphQL query per 25 repos replaces three /search/issues calls each,
    # trading a 30 req/min cap for ~1 point of a 5000/hour budget.
    targets = [
        (cfg.get("owner", defaults.get("owner", "compas-dev")), cfg["name"])
        for cfg in selected_cfgs
    ]
    started = time.time()
    batched_counts = gh.batch_issue_pr_counts(targets)
    if batched_counts:
        print(f"Batched issue/PR counts for {len(batched_counts)}/{len(targets)} repos "
              f"in {time.time() - started:.0f}s", file=sys.stderr)

    print(f"Collecting {scope} repos…", file=sys.stderr)
    repos = []
    for cfg in selected_cfgs:
        try:
            owner = cfg.get("owner", defaults.get("owner", "compas-dev"))
            tree = trees.get((owner, cfg["name"])) if local_scan else None
            scanner = localrepo.LocalScanner(gh, tree) if tree else None
            repos.append(collect_repo(gh, cfg, defaults, features, tracked, scanner=scanner,
                                      counts=batched_counts.get((owner, cfg["name"]))))
        except Exception as exc:  # noqa: BLE001 — fail soft per repo
            gh.warnings.append(f"{cfg.get('name')}: {exc}")
            print(f"    ! {cfg.get('name')} failed: {exc}", file=sys.stderr)

    if temp_scan_dir:
        shutil.rmtree(temp_scan_dir, ignore_errors=True)

    out = root / "site" / "public" / "data.json"
    if requested_names and out.exists():
        try:
            existing_repos = json.loads(out.read_text()).get("repos", [])
        except (json.JSONDecodeError, OSError):
            existing_repos = []
        repos = merge_repos(existing_repos, repos)

    # Deterministic ordering for clean diffs.
    category_order = {c: i for i, c in enumerate(
        ["core", "fabrication", "timber", "geometry", "structures", "fea", "viz", "xr", "ai", "apps", "tooling", "template", "other"]
    )}
    repos.sort(key=lambda r: (category_order.get(r["category"], 99), r["name"]))

    data = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "features": [{"id": f["id"], "label": f["label"], "kind": f.get("kind"), "applies_to": f.get("applies_to")} for f in features],
        "categories": sorted({r["category"] for r in repos}, key=lambda c: category_order.get(c, 99)),
        "repos": repos,
        "warnings": gh.warnings,
        "collection_scope": "partial" if requested_names else "all",
        "collected_repos": sorted(requested_names) if requested_names else None,
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2))
    print(f"Wrote {out} ({len(repos)} repos, {len(gh.warnings)} warnings)", file=sys.stderr)

    if not args.no_history and not requested_names:
        hist_dir = root / "data-history"
        hist_dir.mkdir(exist_ok=True)
        snapshot = build_history_snapshot(data)
        (hist_dir / f"{snapshot['date']}.json").write_text(json.dumps(snapshot, indent=2))
    elif requested_names and not args.no_history:
        print("Skipped history snapshot for partial collection.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
