"""Minimal GitHub REST API client (stdlib only, fail-soft)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

API = "https://api.github.com"


class GitHub:
    def __init__(self, token: Optional[str] = None):
        self.token = token
        self.warnings: list[str] = []

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "compas-mission-control",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get(self, path: str, params: Optional[dict] = None, retries: int = 3) -> Optional[Any]:
        """GET a path (relative to the API root or absolute). Returns parsed JSON or None."""
        url = path if path.startswith("http") else f"{API}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        for attempt in range(retries):
            req = urllib.request.Request(url, headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                if exc.code in (403, 429):
                    # rate limited — respect reset if provided, else back off
                    reset = exc.headers.get("X-RateLimit-Reset")
                    wait = 60
                    if reset:
                        wait = max(1, int(reset) - int(time.time())) + 1
                    wait = min(wait, 90)
                    if attempt < retries - 1:
                        time.sleep(wait)
                        continue
                self.warnings.append(f"GET {url} -> HTTP {exc.code}")
                return None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                self.warnings.append(f"GET {url} -> {exc}")
                return None
        return None

    def graphql(self, query: str, retries: int = 3) -> Optional[dict]:
        """POST a GraphQL query. Returns the `data` object, or None on failure.

        GraphQL rejects unauthenticated requests outright, so callers must be
        prepared to fall back to the REST path when no token is configured.
        """
        if not self.token:
            return None
        body = json.dumps({"query": query}).encode("utf-8")
        headers = {**self._headers(), "Content-Type": "application/json"}
        for attempt in range(retries):
            req = urllib.request.Request(f"{API}/graphql", data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429) and attempt < retries - 1:
                    time.sleep(min(60, 2 ** attempt * 5))
                    continue
                self.warnings.append(f"GraphQL -> HTTP {exc.code}")
                return None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                self.warnings.append(f"GraphQL -> {exc}")
                return None
            # Partial results are normal: a single unreadable repo nulls its own
            # alias while every sibling in the batch still resolves.
            for error in payload.get("errors") or []:
                message = error.get("message", "unknown error")
                if "Could not resolve" not in message:
                    self.warnings.append(f"GraphQL: {message}")
            return payload.get("data")
        return None

    def batch_issue_pr_counts(self, targets: list[tuple[str, str]], chunk: int = 25) -> dict:
        """Open issue/PR counts and oldest-issue age for many repos at once.

        Replaces three /search/issues calls per repo. That endpoint is capped at
        30 requests/minute, which cost roughly seven minutes on a 74-repo run;
        the same information costs about one point of the 5000/hour GraphQL
        budget per chunk. Repos missing from the response are simply absent from
        the result, and the caller falls back to REST for those.
        """
        import datetime

        results: dict[tuple[str, str], dict] = {}
        for start in range(0, len(targets), chunk):
            batch = targets[start:start + chunk]
            fields = []
            for index, (owner, name) in enumerate(batch):
                fields.append(
                    f'  a{index}: repository(owner: "{owner}", name: "{name}") {{\n'
                    f"    issues(states: OPEN) {{ totalCount }}\n"
                    f"    pullRequests(states: OPEN) {{ totalCount }}\n"
                    f"    oldest: issues(states: OPEN, first: 1, "
                    f"orderBy: {{field: CREATED_AT, direction: ASC}}) {{ nodes {{ createdAt }} }}\n"
                    f"  }}"
                )
            data = self.graphql("query {\n" + "\n".join(fields) + "\n}")
            if not data:
                continue
            now = datetime.datetime.now(datetime.timezone.utc)
            for index, key in enumerate(batch):
                node = data.get(f"a{index}")
                if not node:
                    continue
                nodes = ((node.get("oldest") or {}).get("nodes")) or []
                oldest_days = None
                if nodes and nodes[0].get("createdAt"):
                    created = datetime.datetime.fromisoformat(nodes[0]["createdAt"].replace("Z", "+00:00"))
                    oldest_days = (now - created).days
                results[key] = {
                    "open_issues": (node.get("issues") or {}).get("totalCount", 0),
                    "open_prs": (node.get("pullRequests") or {}).get("totalCount", 0),
                    "oldest_open_issue_age_days": oldest_days,
                }
        return results

    # -- convenience wrappers ------------------------------------------------

    def repo(self, owner: str, name: str) -> Optional[dict]:
        return self.get(f"/repos/{owner}/{name}")

    def latest_release(self, owner: str, name: str) -> Optional[dict]:
        return self.get(f"/repos/{owner}/{name}/releases/latest")

    def file_text(self, owner: str, name: str, path: str, ref: Optional[str] = None) -> Optional[str]:
        """Fetch and decode a text file from a repo, or None if missing."""
        import base64

        params = {"ref": ref} if ref else None
        data = self.get(f"/repos/{owner}/{name}/contents/{path}", params=params)
        if not data or "content" not in data:
            return None
        try:
            return base64.b64decode(data["content"]).decode("utf-8")
        except Exception:  # noqa: BLE001
            return None

    def readme_text(self, owner: str, name: str, ref: Optional[str] = None) -> str | bool | None:
        """Return README text, False when absent, or None when the API failed."""
        import base64

        params = {"ref": ref} if ref else None
        warning_count = len(self.warnings)
        data = self.get(f"/repos/{owner}/{name}/readme", params=params)
        if data is None:
            return None if len(self.warnings) > warning_count else False
        try:
            return base64.b64decode(data["content"]).decode("utf-8")
        except (KeyError, ValueError, UnicodeDecodeError):
            self.warnings.append(f"README decode failed for {owner}/{name}")
            return None

    def file_exists(self, owner: str, name: str, path: str, ref: Optional[str] = None) -> Optional[bool]:
        """Return True/False for a repository path, or None when the API failed.

        ``get`` deliberately returns None both for a 404 and for a request
        failure. A request failure records a warning, which lets this wrapper
        preserve the tri-state result needed by feature detection.
        """
        params = {"ref": ref} if ref else None
        warning_count = len(self.warnings)
        data = self.get(f"/repos/{owner}/{name}/contents/{path}", params=params)
        if data is not None:
            return True
        if len(self.warnings) > warning_count:
            return None
        return False

    def dir_entries(self, owner: str, name: str, path: str, ref: Optional[str] = None) -> list[str]:
        params = {"ref": ref} if ref else None
        data = self.get(f"/repos/{owner}/{name}/contents/{path}", params=params)
        if isinstance(data, list):
            return [e.get("name", "") for e in data]
        return []

    def ci_status(self, owner: str, name: str, branch: str) -> str:
        """passing | failing | none, based on the latest completed run on the branch."""
        data = self.get(
            f"/repos/{owner}/{name}/actions/runs",
            params={"branch": branch, "status": "completed", "per_page": 1},
        )
        runs = (data or {}).get("workflow_runs") or []
        if not runs:
            return "none"
        return "passing" if runs[0].get("conclusion") == "success" else "failing"

    def issue_pr_counts(self, owner: str, name: str) -> dict:
        """Separate open issues from open PRs, plus oldest open issue age (days)."""
        import datetime

        def count(query: str) -> int:
            data = self.get("/search/issues", params={"q": query, "per_page": 1})
            return int((data or {}).get("total_count", 0)) if data is not None else 0

        open_prs = count(f"repo:{owner}/{name} is:pr is:open")
        open_issues = count(f"repo:{owner}/{name} is:issue is:open")

        oldest_days = None
        data = self.get(
            "/search/issues",
            params={"q": f"repo:{owner}/{name} is:issue is:open", "sort": "created", "order": "asc", "per_page": 1},
        )
        items = (data or {}).get("items") or []
        if items:
            created = items[0].get("created_at")
            if created:
                dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
                oldest_days = (datetime.datetime.now(datetime.timezone.utc) - dt).days

        return {"open_issues": open_issues, "open_prs": open_prs, "oldest_open_issue_age_days": oldest_days}

    def search_code(self, owner: str, name: str, pattern: str, language: Optional[str] = None) -> Optional[bool]:
        """True/False if a code pattern appears in the repo, or None on error."""
        q = f'"{pattern}" repo:{owner}/{name}'
        if language:
            q += f" language:{language}"
        data = self.get("/search/code", params={"q": q, "per_page": 1})
        if data is None:
            return None
        return int(data.get("total_count", 0)) > 0
