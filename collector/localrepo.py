"""Local repository scanning: fetch a source tarball, then answer file/tree
questions from disk instead of the GitHub API. See SPEC.md §5.4.

Motivation: the ``code`` feature kind used /search/code, which is capped at 10
requests/minute. ~740 queries per run meant over an hour of pure rate-limit
waiting, and exhausted retries were recorded as ``unknown`` — the adoption
matrix flickered from run to run. A source tarball is one request and answers
every content question locally, exactly and repeatably.
"""

from __future__ import annotations

import fnmatch
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator, Optional

API = "https://api.github.com"

# GitHub code search accepts a `language:` qualifier; locally we approximate it
# with the file extensions that language owns. Keys are lowercased.
LANGUAGE_GLOBS: dict[str, tuple[str, ...]] = {
    "python": ("*.py", "*.pyi"),
    "yaml": ("*.yml", "*.yaml"),
    "toml": ("*.toml",),
    "markdown": ("*.md", "*.markdown"),
    "json": ("*.json",),
    "javascript": ("*.js", "*.mjs", "*.cjs"),
    "typescript": ("*.ts", "*.tsx"),
    "c++": ("*.cpp", "*.hpp", "*.cc", "*.hh", "*.h"),
    "c": ("*.c", "*.h"),
}

# Never scanned: version-control internals and vendored dependency trees, which
# GitHub's index also leaves out. Keeping this list short keeps local results
# comparable to the API results they replace.
SKIP_DIRS = {".git", "node_modules", ".tox", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache"}

# Files larger than this are skipped, mirroring the API index's own cutoff and
# keeping a stray binary or data blob from dominating a scan.
MAX_FILE_BYTES = 2_000_000


def _strip_root(members: Iterator[tarfile.TarInfo]) -> Iterator[tarfile.TarInfo]:
    """Drop the ``{owner}-{repo}-{sha}/`` prefix GitHub wraps tarballs in."""
    for member in members:
        parts = member.name.split("/", 1)
        if len(parts) != 2 or not parts[1]:
            continue
        member.name = parts[1]
        yield member


def fetch_tree(owner: str, name: str, ref: Optional[str], token: Optional[str], dest: Path) -> Optional[Path]:
    """Download and extract a repo's source tarball. Returns the tree root, or None."""
    ref_part = f"/{ref}" if ref else ""
    url = f"{API}/repos/{owner}/{name}/tarball{ref_part}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "compas-mission-control",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    target = dest / f"{owner}__{name}"
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=120) as response:
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=True) as tmp:
                shutil.copyfileobj(response, tmp)
                tmp.flush()
                tmp.seek(0)
                with tarfile.open(tmp.name, "r:gz") as archive:
                    # filter="data" refuses absolute paths and traversal outside
                    # the destination (Python 3.12+); older runtimes fall back.
                    try:
                        archive.extractall(target, members=_strip_root(archive), filter="data")
                    except TypeError:
                        archive.extractall(target, members=_strip_root(archive))
    except (urllib.error.URLError, urllib.error.HTTPError, tarfile.TarError, OSError, TimeoutError):
        shutil.rmtree(target, ignore_errors=True)
        return None
    return target


def prefetch(entries: list[tuple[str, str, Optional[str]]], token: Optional[str], dest: Path,
             workers: int = 8, log=None) -> dict[tuple[str, str], Path]:
    """Fetch many trees concurrently. Returns {(owner, name): tree_root} for successes.

    Concurrency is the whole point: a serial pass over ~74 repositories spends
    minutes in transfer latency, while the tarball endpoint bills one ordinary
    REST request each against a 5000/hour budget.
    """
    trees: dict[tuple[str, str], Path] = {}
    dest.mkdir(parents=True, exist_ok=True)

    def one(entry):
        owner, name, ref = entry
        return (owner, name), fetch_tree(owner, name, ref, token, dest)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for key, path in pool.map(one, entries):
            if path is not None:
                trees[key] = path
            elif log:
                log(f"    ! tarball fetch failed for {key[0]}/{key[1]}")
    return trees


class LocalScanner:
    """Answers content questions from an extracted tree; delegates the rest to GitHub.

    Deliberately mirrors the ``GitHub`` method names used by the feature engine
    so ``detect`` and ``detect_hosts`` work against either object unchanged.
    Anything not overridden here (repo metadata, CI, releases, issue counts)
    falls through to the wrapped client.
    """

    def __init__(self, gh, root: Path):
        self._gh = gh
        self.root = root

    def __getattr__(self, item):
        # Only reached for attributes this class does not define, so network
        # calls that have no local equivalent keep working untouched.
        return getattr(self._gh, item)

    # -- path helpers --------------------------------------------------------

    def _resolve(self, path: str) -> Optional[Path]:
        candidate = (self.root / path).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            return None  # escaped the tree
        return candidate

    # -- GitHub-compatible surface -------------------------------------------

    def file_text(self, owner: str, name: str, path: str, ref: Optional[str] = None) -> Optional[str]:
        target = self._resolve(path)
        if target is None or not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def file_exists(self, owner: str, name: str, path: str, ref: Optional[str] = None) -> Optional[bool]:
        target = self._resolve(path)
        # A local tree is authoritative: absence is a fact, never a failed
        # request, so this never returns the API client's ``None``.
        return bool(target is not None and target.exists())

    def readme_text(self, owner: str, name: str, ref: Optional[str] = None):
        for candidate in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
            text = self.file_text(owner, name, candidate)
            if text is not None:
                return text
        # Case-insensitive fallback for repos with unusual capitalisation.
        for entry in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            if entry.is_file() and entry.name.lower().startswith("readme"):
                try:
                    return entry.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
        return False

    def dir_entries(self, owner: str, name: str, path: str, ref: Optional[str] = None) -> list[str]:
        target = self._resolve(path)
        if target is None or not target.is_dir():
            return []
        return sorted(entry.name for entry in target.iterdir())

    def _iter_files(self, globs: Optional[tuple[str, ...]]) -> Iterator[Path]:
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if globs and not any(fnmatch.fnmatch(path.name, g) for g in globs):
                continue
            yield path

    def grep_files(self, pattern: str, language: Optional[str] = None) -> list[str]:
        """Return repo-relative paths whose bytes contain ``pattern`` literally.

        Byte-level matching sidesteps decode errors on stray binaries and, more
        importantly, is a true substring test — unlike the API's tokenised
        search, which never matched dotted patterns such as ``compas.scene.``
        the way features.yml reads as though it does.
        """
        globs = LANGUAGE_GLOBS.get((language or "").lower()) if language else None
        needle = pattern.encode("utf-8")
        hits: list[str] = []
        for path in self._iter_files(globs):
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                if needle in path.read_bytes():
                    hits.append(str(path.relative_to(self.root)))
            except OSError:
                continue
        return sorted(hits)

    def search_code(self, owner: str, name: str, pattern: str, language: Optional[str] = None) -> Optional[bool]:
        return bool(self.grep_files(pattern, language))

    def count_code(self, pattern: str, language: Optional[str] = None) -> int:
        return len(self.grep_files(pattern, language))
