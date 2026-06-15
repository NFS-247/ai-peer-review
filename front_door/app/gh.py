"""Minimal GitHub REST client (stdlib urllib).

Reads the board data (PRs, labels, comments, ledger issues) and posts operator
command comments. Two identities are used by the app: a read token for the
board, and the operator's own token for writes (see README auth section) — both
are just a ``GitHub`` constructed with the right token.

Kept tiny and dependency-free on purpose; mockable in tests by patching
``_request`` or ``urlopen``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional


class GitHubError(RuntimeError):
    """A failed GitHub call. ``status`` is the HTTP status code when the failure
    was an HTTP error response (None for transport/URL errors), so callers can
    branch on it explicitly instead of string-matching the message — e.g.
    distinguishing a 404 (not found) from a 403 (no access) on a provisioning
    path where that difference is load-bearing."""

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class GitHub:
    def __init__(self, token: str, *, api_base: str = "https://api.github.com", timeout: int = 20) -> None:
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        url = path if path.startswith("http") else f"{self._api_base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise GitHubError(
                f"GitHub {method} {path} -> HTTP {exc.code}: {detail[:300]}",
                status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise GitHubError(f"GitHub {method} {path} failed: {exc.reason}") from exc
        return json.loads(raw) if raw else None

    def _paginate(self, path: str, *, per_page: int = 100, max_pages: int = 20) -> list:
        """Follow page=1.. until a short/empty page (no Link-header parsing
        needed). ``max_pages`` caps the walk so a misbehaving endpoint can't loop
        forever; 20*100 = 2000 items is ample for a board read."""
        sep = "&" if "?" in path else "?"
        out: list = []
        for page in range(1, max_pages + 1):
            chunk = self._request("GET", f"{path}{sep}per_page={per_page}&page={page}")
            if not isinstance(chunk, list) or not chunk:
                break
            out.extend(chunk)
            if len(chunk) < per_page:
                break
        return out

    # ---- reads --------------------------------------------------------------
    def authenticated_login(self) -> str:
        data = self._request("GET", "/user") or {}
        return data.get("login", "")

    def list_open_pulls(self, repo: str) -> list[dict]:
        return self._paginate(f"/repos/{repo}/pulls?state=open")

    def list_labels(self, repo: str, number: int) -> list[str]:
        data = self._paginate(f"/repos/{repo}/issues/{number}/labels")
        return [l.get("name", "") for l in data]

    def list_issue_comments(self, repo: str, number: int) -> list[dict]:
        return self._paginate(f"/repos/{repo}/issues/{number}/comments")

    def list_issue_comments_for_repo(self, repo: str) -> list[dict]:
        """Every issue/PR comment in the repo in ONE paginated sweep (newest
        first), so the board groups by PR instead of making a separate call per
        PR — avoids the N-per-PR fan-out that trips GitHub's secondary rate
        limit. Each comment carries an ``issue_url`` identifying its PR/issue."""
        return self._paginate(f"/repos/{repo}/issues/comments?sort=created&direction=desc")

    def list_open_issues(self, repo: str) -> list[dict]:
        # issues endpoint also returns PRs; filter to true issues by caller need.
        return self._paginate(f"/repos/{repo}/issues?state=open")

    def find_issue_body_by_marker(self, repo: str, marker: str) -> str:
        """Body of the first open issue whose body contains ``marker`` (else '')."""
        for issue in self.list_open_issues(repo):
            if "pull_request" in issue:
                continue
            body = issue.get("body") or ""
            if marker in body:
                return body
        return ""

    # ---- writes (operator identity) -----------------------------------------
    def post_issue_comment(self, repo: str, number: int, body: str) -> dict:
        return self._request(
            "POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}
        ) or {}


__all__ = ["GitHub", "GitHubError"]
