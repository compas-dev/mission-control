"""Feature-detection engine. See SPEC.md §5.4.

Given a repo's already-collected packaging data and a GitHub client, compute the
adoption status of each feature: adopted | not-adopted | n/a | unknown.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from parse import same_version, satisfies

STATUS_ADOPTED = "adopted"
STATUS_NOT = "not-adopted"
STATUS_NA = "n/a"
STATUS_UNKNOWN = "unknown"


_MARKDOWN_LINKED_IMAGE = re.compile(
    r"\[!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]+)\)\]\((?P<href>[^)]+)\)"
)


class _LinkedImageHTMLParser(HTMLParser):
    """Collect images nested inside links, independent of HTML formatting."""

    def __init__(self) -> None:
        super().__init__()
        self._link_targets: list[str | None] = []
        self.linked_images: set[tuple[str, str, str]] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "a":
            self._link_targets.append(attributes.get("href"))
        elif tag == "img" and self._link_targets and self._link_targets[-1]:
            src = attributes.get("src")
            if src:
                self.linked_images.add(
                    (attributes.get("alt") or "", src, self._link_targets[-1] or "")
                )

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link_targets:
            self._link_targets.pop()


def _html_linked_images(content: str) -> set[tuple[str, str, str]]:
    parser = _LinkedImageHTMLParser()
    parser.feed(content)
    return parser.linked_images


def _markdown_linked_image(pattern: str) -> tuple[str, str, str] | None:
    match = _MARKDOWN_LINKED_IMAGE.fullmatch(pattern)
    if not match:
        return None
    return match.group("alt"), match.group("src"), match.group("href")


def _cell(status: str, source: str = "auto", detail: str = "") -> dict:
    return {"status": status, "source": source, "detail": detail}


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

    runtime = repo_cfg.get("runtime", "python")
    applies_to = feature.get("applies_to")
    if isinstance(applies_to, str):
        applies_to = [applies_to]
    if applies_to and runtime not in applies_to:
        return _cell(STATUS_NA, detail=f"not applicable to {runtime} projects")

    # -- deployment checks (use already-collected release data / conda-forge) --
    if kind in ("registry-match", "pypi-match"):
        distributions = (release or {}).get("distributions") or []
        if kind == "pypi-match":
            distributions = [d for d in distributions if d.get("registry") == "pypi"]
        if not distributions:
            label = "PyPI" if kind == "pypi-match" else "a package registry"
            return _cell(STATUS_NA, detail=f"not distributed through {label}")
        rel = release or {}
        gt = rel.get("github_tag")
        published = [d for d in distributions if d.get("version")]
        if gt and published:
            details = ", ".join(f"{d['registry']} {d['version']}" for d in published)
            if all(same_version(gt, d["version"]) for d in published):
                return _cell(STATUS_ADOPTED, detail=f"{details} = GitHub {gt}")
            return _cell(STATUS_NOT, detail=f"{details} ≠ GitHub {gt}")
        if gt and not published:
            registries = ", ".join(d["registry"] for d in distributions)
            return _cell(STATUS_NOT, detail=f"GitHub {gt}, not found on {registries}")
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
        if runtime != "python":
            return _cell(STATUS_NA, detail=f"not applicable to {runtime} projects")
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

    if kind == "readme":
        content = gh.readme_text(owner, name, repo_cfg.get("branch"))
        if content is None:
            return _cell(STATUS_UNKNOWN, detail="README check failed")
        if content is False:
            return _cell(STATUS_NOT, detail="README not found")
        patterns = [pattern.format(owner=owner, name=name) for pattern in detect_cfg.get("present", [])]
        html_linked_images = None
        for pattern in patterns:
            if pattern in content:
                return _cell(STATUS_ADOPTED, detail="badge links to Mission Control")
            linked_image = _markdown_linked_image(pattern)
            if linked_image:
                if html_linked_images is None:
                    html_linked_images = _html_linked_images(content)
                if linked_image in html_linked_images:
                    return _cell(STATUS_ADOPTED, detail="badge links to Mission Control")
        return _cell(STATUS_NOT, detail="linked COMPAS badge not found in README")

    if kind == "code":
        present = detect_cfg.get("present", [])
        absent = detect_cfg.get("absent", [])
        language = detect_cfg.get("language")
        # A local scanner can report how many files match, which turns a
        # red/green cell into a burndown number for the remaining call sites.
        counter = getattr(gh, "count_code", None)

        def files_matching(pattern: str) -> str:
            if counter is None:
                return ""
            hits = counter(pattern, language)
            return f" in {hits} file{'s' if hits != 1 else ''}"

        matched_present = None
        for pat in present:
            found = gh.search_code(owner, name, pat, language=language)
            if found is None:
                return _cell(STATUS_UNKNOWN, detail="code search failed")
            if found:
                matched_present = pat
                break
        for pat in absent:
            found = gh.search_code(owner, name, pat, language=language)
            if found is None:
                return _cell(STATUS_UNKNOWN, detail="code search failed")
            if found:
                return _cell(STATUS_NOT, detail=f"still uses {pat!r}{files_matching(pat)}")
        if present:
            if matched_present:
                return _cell(STATUS_ADOPTED, detail=f"matched {matched_present!r}{files_matching(matched_present)}")
            no_match = detect_cfg.get("no_match", STATUS_NOT)
            if no_match == STATUS_NA:
                return _cell(STATUS_NA, detail="no matching API usage")
            return _cell(STATUS_NOT, detail="no match")
        if absent:
            return _cell(STATUS_ADOPTED, detail="clean")
        return _cell(STATUS_UNKNOWN)

    # manual with no override
    return _cell(STATUS_UNKNOWN, source="manual", detail="not set")
