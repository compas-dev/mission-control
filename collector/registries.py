"""Package-registry lookups shared by Python and Node projects.

All lookups are fail-soft and return ``None`` when a package is missing or the
registry is unavailable. The collector can therefore keep rendering GitHub
health data even when a package registry is down.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

import pypi

USER_AGENT = "compas-mission-control"


def _json(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _npm_latest(name: str) -> Optional[dict]:
    encoded = urllib.parse.quote(name, safe="")
    data = _json(f"https://registry.npmjs.org/{encoded}")
    if not data:
        return None
    version = (data.get("dist-tags") or {}).get("latest")
    if not version:
        return None
    date = ((data.get("time") or {}).get(version) or "")[:10] or None
    return {"version": version, "date": date}


def _jsr_latest(name: str) -> Optional[dict]:
    # JSR package identifiers are always scoped: @scope/package.
    if not name.startswith("@") or "/" not in name:
        return None
    data = _json(f"https://jsr.io/{name}/meta.json")
    if not data:
        return None
    version = data.get("latest")
    if not version:
        return None
    date = (((data.get("versions") or {}).get(version) or {}).get("createdAt") or "")[:10] or None
    return {"version": version, "date": date}


def latest(kind: str, name: str) -> Optional[dict]:
    """Return ``{version, date}`` for a supported package registry."""
    if kind == "pypi":
        return pypi.latest(name)
    if kind == "npm":
        return _npm_latest(name)
    if kind == "jsr":
        return _jsr_latest(name)
    return None


def package_url(kind: str, name: str) -> Optional[str]:
    """Return the human-facing package page for a supported registry."""
    if kind == "pypi":
        return f"https://pypi.org/project/{name}/"
    if kind == "npm":
        return f"https://www.npmjs.com/package/{name}"
    if kind == "jsr":
        return f"https://jsr.io/{name}"
    return None
