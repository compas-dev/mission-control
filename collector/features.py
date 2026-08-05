"""Feature-detection engine. See SPEC.md §5.4.

Given a repo's already-collected packaging data and a GitHub client, compute the
adoption status of each feature: adopted | not-adopted | n/a | unknown.
"""

from __future__ import annotations

from packaging.version import Version

from parse import satisfies

STATUS_ADOPTED = "adopted"
STATUS_NOT = "not-adopted"
STATUS_NA = "n/a"
STATUS_UNKNOWN = "unknown"


def _cell(status: str, source: str = "auto", detail: str = "") -> dict:
    return {"status": status, "source": source, "detail": detail}


def _same_version(a: str, b: str) -> bool:
    """Compare semantically so PyPI's '1.0.0rc0' matches a GitHub '1.0.0-rc0' tag."""
    try:
        return Version(a) == Version(b)
    except Exception:  # noqa: BLE001 — non-PEP440 tags fall back to string compare
        return a == b


def detect(feature: dict, repo_cfg: dict, packaging: dict, gh, owner: str, name: str,
           release: dict | None = None, conda=None) -> dict:
    """Return a single adoption cell for (feature, repo)."""
    fid = feature["id"]
    kind = feature.get("kind", "manual")
    detect_cfg = feature.get("detect", {}) or {}

    # Manual override always wins.
    overrides = repo_cfg.get("feature_overrides") or {}
    if fid in overrides:
        return _cell(overrides[fid], source="manual", detail="manual override")

    # -- deployment checks (use already-collected release data / conda-forge) --
    if kind == "pypi-match":
        if not repo_cfg.get("pypi"):
            return _cell(STATUS_NA, detail="not distributed on PyPI")
        rel = release or {}
        pv, gt = rel.get("pypi_version"), rel.get("github_tag")
        if pv and gt:
            if _same_version(pv, gt):
                return _cell(STATUS_ADOPTED, detail=f"PyPI {pv} = GitHub {gt}")
            return _cell(STATUS_NOT, detail=f"PyPI {pv} ≠ GitHub {gt}")
        if gt and not pv:
            return _cell(STATUS_NOT, detail=f"GitHub {gt}, not on PyPI")
        return _cell(STATUS_UNKNOWN, detail="no comparable release")

    if kind == "conda":
        pkg = repo_cfg.get("pypi") or name
        info = conda.latest(pkg) if conda else None
        if info:
            return _cell(STATUS_ADOPTED, detail=f"conda-forge {info['version']}")
        if not repo_cfg.get("pypi"):
            return _cell(STATUS_NA, detail="not distributed on PyPI/conda")
        return _cell(STATUS_NOT, detail="no conda-forge package")

    if kind == "pin":
        pkg = detect_cfg.get("package", "")
        deps = packaging.get("requirements", {})
        spec = deps.get(pkg.lower())
        if spec is None:
            # compas core itself, or a repo that doesn't depend on the package
            if name == pkg:
                return _cell(STATUS_NA, detail="is the package")
            return _cell(STATUS_NA, detail=f"no {pkg} dependency")
        ok = satisfies(spec, detect_cfg.get("satisfied_by", ""))
        if ok is None:
            return _cell(STATUS_UNKNOWN, detail=f"{pkg} {spec}")
        return _cell(STATUS_ADOPTED if ok else STATUS_NOT, detail=f"{pkg} {spec}")

    if kind == "python":
        version = detect_cfg.get("version", "")
        versions = packaging.get("python_versions") or []
        source = packaging.get("python_source", "unknown")
        if not versions and source == "unknown":
            return _cell(STATUS_UNKNOWN, detail="no python info")
        adopted = version in versions
        support = ", ".join(versions) or "none of the tracked versions"
        return _cell(STATUS_ADOPTED if adopted else STATUS_NOT, detail=f"{source}: {support}")

    if kind == "file":
        branch = repo_cfg.get("branch")
        any_of = detect_cfg.get("any_of", [])
        none_of = detect_cfg.get("none_of", [])
        results = {p: gh.file_exists(owner, name, p, branch) for p in any_of + none_of}
        present = [p for p in any_of if results[p] is True]
        forbidden = [p for p in none_of if results[p] is True]
        unavailable_required = [p for p in any_of if results[p] is None]
        unavailable_forbidden = [p for p in none_of if results[p] is None]
        # Adopted requires the wanted file(s) present AND the unwanted ones gone.
        # This expresses "migrated TO x and AWAY from y" (e.g. MkDocs, not Sphinx).
        has_required = not any_of or bool(present)
        if forbidden:
            return _cell(STATUS_NOT, detail=f"still has {', '.join(forbidden)}")
        if any_of and not present and unavailable_required:
            return _cell(STATUS_UNKNOWN, detail=f"file check failed: {', '.join(unavailable_required)}")
        if has_required and unavailable_forbidden:
            return _cell(STATUS_UNKNOWN, detail=f"file check failed: {', '.join(unavailable_forbidden)}")
        if has_required and not forbidden:
            bits = []
            if present:
                bits.append("has " + ", ".join(present))
            if none_of:
                bits.append("no " + ", ".join(none_of))
            return _cell(STATUS_ADOPTED, detail="; ".join(bits) or "ok")
        if any_of and not present:
            return _cell(STATUS_NOT, detail=f"missing {' / '.join(any_of)}")
        return _cell(STATUS_NOT, detail="file absent")

    if kind == "code":
        present = detect_cfg.get("present", [])
        absent = detect_cfg.get("absent", [])
        language = detect_cfg.get("language")
        for pat in present:
            found = gh.search_code(owner, name, pat, language=language)
            if found is None:
                return _cell(STATUS_UNKNOWN, detail="code search failed")
            if found:
                return _cell(STATUS_ADOPTED, detail=f"matched {pat!r}")
        for pat in absent:
            found = gh.search_code(owner, name, pat, language=language)
            if found is None:
                return _cell(STATUS_UNKNOWN, detail="code search failed")
            if found:
                return _cell(STATUS_NOT, detail=f"still uses {pat!r}")
        if present:
            return _cell(STATUS_NOT, detail="no match")
        if absent:
            return _cell(STATUS_ADOPTED, detail="clean")
        return _cell(STATUS_UNKNOWN)

    # manual with no override
    return _cell(STATUS_UNKNOWN, source="manual", detail="not set")
