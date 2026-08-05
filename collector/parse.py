"""Parsing of packaging metadata: dependencies, Python versions, host apps.

All functions are fail-soft: bad input yields empty/None rather than raising.
"""

from __future__ import annotations

import re
import tomllib
from typing import Optional

import yaml
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

# Python versions we care to display as matrix columns.
KNOWN_PYTHONS = ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13"]


def parse_requirements(text: Optional[str]) -> dict[str, str]:
    """Map of package name -> raw specifier string from a requirements.txt."""
    result: dict[str, str] = {}
    if not text:
        return result
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            req = Requirement(line)
            result[req.name.lower()] = str(req.specifier) or "*"
        except Exception:  # noqa: BLE001
            continue
    return result


def parse_environment_yml(text: Optional[str]) -> dict[str, str]:
    """Dependencies from a conda environment.yml (name -> specifier).

    Conda-style entries look like 'compas >=2.4'; a nested `pip:` list holds
    PEP 508 requirements. Some repos (e.g. compas_cra) declare their real deps
    only here, so it's a third dependency source alongside requirements.txt and
    pyproject.
    """
    result: dict[str, str] = {}
    if not text:
        return result
    try:
        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001
        return result
    if not isinstance(data, dict):
        return result
    for entry in data.get("dependencies", []) or []:
        if isinstance(entry, str):
            m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*(.*)$", entry)
            if m:
                result[m.group(1).lower()] = m.group(2).strip() or "*"
        elif isinstance(entry, dict):  # the `pip:` sub-list
            for pip_dep in entry.get("pip", []) or []:
                try:
                    req = Requirement(pip_dep)
                    result[req.name.lower()] = str(req.specifier) or "*"
                except Exception:  # noqa: BLE001 — skip '-e .', paths, etc.
                    continue
    return result


def canonical_name(name: Optional[str]) -> str:
    """PEP 503 canonical form so 'compas_model', 'compas-model', 'Compas.Model' match."""
    if not name:
        return ""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def dependency_floor_major(specifier: str) -> Optional[int]:
    """Best-effort major version of the lower bound of a specifier ('>=2.3,<3' -> 2)."""
    if not specifier:
        return None
    for part in specifier.split(","):
        m = re.search(r">=?\s*(\d+)", part)
        if m:
            return int(m.group(1))
    return None


def satisfies(specifier: str, satisfied_by: str) -> Optional[bool]:
    """Does the specifier's lower bound fall within the `satisfied_by` range?

    Used by `pin` features, e.g. specifier 'compas >= 2.3, < 3' with
    satisfied_by '>=2,<3' -> True.
    """
    major = dependency_floor_major(specifier)
    if major is None:
        return None
    try:
        return Version(f"{major}.0") in SpecifierSet(satisfied_by)
    except Exception:  # noqa: BLE001
        return None


def parse_pyproject(text: Optional[str]) -> dict:
    """Extract name, requires-python, classifier Python versions, and deps.

    `dependencies` mirrors `parse_requirements`' shape (name -> specifier) so the
    two sources can be merged. Newer repos declare static deps here rather than in
    requirements.txt, so the compas pin often lives in this table.
    """
    out = {"name": None, "requires_python": None, "classifier_pythons": [], "dependencies": {}}
    if not text:
        return out
    try:
        data = tomllib.loads(text)
    except Exception:  # noqa: BLE001
        return out
    project = data.get("project", {})
    out["name"] = project.get("name")
    out["requires_python"] = project.get("requires-python")
    pys = []
    for c in project.get("classifiers", []):
        m = re.match(r"Programming Language :: Python :: (\d+\.\d+)$", c)
        if m:
            pys.append(m.group(1))
    out["classifier_pythons"] = pys
    deps: dict[str, str] = {}
    for spec in project.get("dependencies", []):
        try:
            req = Requirement(spec)
            deps[req.name.lower()] = str(req.specifier) or "*"
        except Exception:  # noqa: BLE001
            continue
    out["dependencies"] = deps
    return out


def parse_ci_pythons(workflow_texts: list[str]) -> list[str]:
    """Pull Python versions out of CI workflow matrices (authoritative source).

    Looks for `python-version:` / `python:` list entries in the YAML text. Kept
    regex-based (rather than a YAML parse) so it survives the many small syntax
    variations across repos.
    """
    versions: set[str] = set()
    for text in workflow_texts:
        if not text:
            continue
        # inline lists: python-version: [3.9, "3.10", '3.11']
        for m in re.finditer(r"python(?:-version)?\s*:\s*\[([^\]]+)\]", text):
            for tok in re.findall(r"\d+\.\d+", m.group(1)):
                versions.add(tok)
        # block lists:
        #   python-version:
        #     - "3.9"
        for block in re.finditer(r"python(?:-version)?\s*:\s*\n((?:\s*-\s*['\"]?\d+\.\d+['\"]?\s*\n?)+)", text):
            for tok in re.findall(r"\d+\.\d+", block.group(1)):
                versions.add(tok)
    return sorted(versions, key=lambda v: [int(x) for x in v.split(".")])


def resolve_pythons(ci: list[str], classifiers: list[str], requires_python: Optional[str]) -> dict:
    """Resolve declared Python support, then fall back to supporting signals.

    ``requires-python`` is the package's normative compatibility declaration.
    A CI matrix is only a list of tested interpreters and is commonly minimal,
    so treating it as an exhaustive support list produces false negatives for
    newer Python versions.
    """
    if requires_python:
        try:
            spec = SpecifierSet(requires_python)
            supported = [v for v in KNOWN_PYTHONS if Version(v) in spec]
            return {"versions": supported, "source": "requires-python"}
        except Exception:  # noqa: BLE001
            pass
    if classifiers:
        return {"versions": classifiers, "source": "classifiers"}
    if ci:
        return {"versions": ci, "source": "ci-matrix"}
    return {"versions": [], "source": "unknown"}


def detect_hosts(gh, owner: str, name: str, branch: str, workflow_names: list[str]) -> dict:
    """Heuristic host-app support from package dirs and publish workflows."""
    src = gh.dir_entries(owner, name, "src", branch)
    joined = " ".join(src).lower()
    wf = " ".join(workflow_names).lower()
    return {
        "rhino": any("_rhino" in s for s in src) or "yak" in wf,
        "ghpython": any("_ghpython" in s for s in src) or "yak" in wf,
        "blender": any("_blender" in s for s in src),
    } if src or wf else {"rhino": None, "ghpython": None, "blender": None}
