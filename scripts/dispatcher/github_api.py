"""Thin wrappers over the GitHub REST API.

Used by the dispatcher to:
- Fetch a PR's changed files and unified diff
- Fetch CI / check-run status
- Read existing PR comments (for verdicts and operator commands)
- Post new PR comments
- Read PR labels (for round counter and tier)
- Set PR labels
- Close a PR (OPERATOR KILL)
- Submit an APPROVE or REQUEST_CHANGES review (OPERATOR APPROVE/BLOCK)

The dispatcher never calls the merge endpoint. See Section 10 and 11.5 of
the design doc: merge capability is structurally absent from this module
and from the workflow permissions.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .redact import redact


GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class PRComment:
    id: int
    body: str
    author_login: str
    author_id: int
    created_at: str


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str  # "queued", "in_progress", "completed"
    conclusion: Optional[str]  # "success", "failure", "neutral", etc.


class GitHubAPI:
    """Tiny GitHub REST client. Deliberately minimal."""

    def __init__(self, token: str, owner: str, repo: str) -> None:
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        self._token = token
        self._owner = owner
        self._repo = repo

    # ---- internals -------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        accept: str = "application/vnd.github+json",
        raw: bool = False,
    ) -> object:
        url = f"{GITHUB_API}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": accept,
                "User-Agent": "TradeWatcher-Dispatcher/1.0",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise RuntimeError(
                f"GitHub API {method} {path} HTTP {exc.code}: {detail[:500]}"
            ) from exc

        if raw:
            return payload_bytes.decode("utf-8", errors="replace")
        return json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}

    # ---- PR data ---------------------------------------------------------

    def get_pr(self, pr_number: int) -> dict:
        return self._request("GET", f"/repos/{self._owner}/{self._repo}/pulls/{pr_number}")

    def get_pr_diff(self, pr_number: int) -> str:
        return self._request(
            "GET",
            f"/repos/{self._owner}/{self._repo}/pulls/{pr_number}",
            accept="application/vnd.github.v3.diff",
            raw=True,
        )

    def get_pr_files(self, pr_number: int) -> list[str]:
        out: list[str] = []
        page = 1
        while True:
            page_data = self._request(
                "GET",
                f"/repos/{self._owner}/{self._repo}/pulls/{pr_number}/files"
                f"?per_page=100&page={page}",
            )
            if not isinstance(page_data, list) or not page_data:
                break
            out.extend(item.get("filename", "") for item in page_data)
            if len(page_data) < 100:
                break
            page += 1
        return [p for p in out if p]

    # ---- comments --------------------------------------------------------

    def list_pr_comments(self, pr_number: int) -> list[PRComment]:
        out: list[PRComment] = []
        page = 1
        while True:
            page_data = self._request(
                "GET",
                f"/repos/{self._owner}/{self._repo}/issues/{pr_number}/comments"
                f"?per_page=100&page={page}",
            )
            if not isinstance(page_data, list) or not page_data:
                break
            for c in page_data:
                user = c.get("user", {}) or {}
                out.append(
                    PRComment(
                        id=int(c.get("id", 0)),
                        body=c.get("body", "") or "",
                        author_login=user.get("login", ""),
                        author_id=int(user.get("id", 0)),
                        created_at=c.get("created_at", ""),
                    )
                )
            if len(page_data) < 100:
                break
            page += 1
        return out

    def post_comment(self, pr_number: int, body: str) -> dict:
        # Redact any secret-shaped or registered-secret text before posting.
        # This is the single choke point through which all dispatcher comments
        # flow, so no API key can leak into a PR comment from any call site.
        return self._request(
            "POST",
            f"/repos/{self._owner}/{self._repo}/issues/{pr_number}/comments",
            body={"body": redact(body)},
        )

    # ---- labels ----------------------------------------------------------

    def list_labels(self, pr_number: int) -> list[str]:
        data = self._request(
            "GET",
            f"/repos/{self._owner}/{self._repo}/issues/{pr_number}/labels",
        )
        return [item.get("name", "") for item in data] if isinstance(data, list) else []

    def add_labels(self, pr_number: int, labels: list[str]) -> dict:
        return self._request(
            "POST",
            f"/repos/{self._owner}/{self._repo}/issues/{pr_number}/labels",
            body={"labels": labels},
        )

    def remove_label(self, pr_number: int, label: str) -> None:
        encoded = urllib.parse.quote(label, safe="")
        try:
            self._request(
                "DELETE",
                f"/repos/{self._owner}/{self._repo}/issues/{pr_number}/labels/{encoded}",
            )
        except RuntimeError as exc:
            # 404 means the label wasn't there; that's fine.
            if "HTTP 404" not in str(exc):
                raise

    # ---- checks ----------------------------------------------------------

    def list_check_runs(self, commit_sha: str) -> list[CheckRun]:
        data = self._request(
            "GET",
            f"/repos/{self._owner}/{self._repo}/commits/{commit_sha}/check-runs",
        )
        runs = data.get("check_runs", []) if isinstance(data, dict) else []
        return [
            CheckRun(
                name=r.get("name", ""),
                status=r.get("status", ""),
                conclusion=r.get("conclusion"),
            )
            for r in runs
        ]

    # ---- reviews ---------------------------------------------------------

    def submit_review(
        self,
        pr_number: int,
        *,
        event: str,
        body: str = "",
    ) -> dict:
        """Submit an APPROVE / REQUEST_CHANGES / COMMENT review.

        Does NOT merge. Merge is a separate API call (PUT
        /pulls/{number}/merge) which this client deliberately does not expose.
        See Section 10 of the design doc.
        """
        if event not in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}:
            raise ValueError(f"event must be APPROVE/REQUEST_CHANGES/COMMENT, got {event!r}")
        return self._request(
            "POST",
            f"/repos/{self._owner}/{self._repo}/pulls/{pr_number}/reviews",
            body={"event": event, "body": redact(body)},
        )

    # ---- PR lifecycle ----------------------------------------------------

    def close_pr(self, pr_number: int) -> dict:
        return self._request(
            "PATCH",
            f"/repos/{self._owner}/{self._repo}/pulls/{pr_number}",
            body={"state": "closed"},
        )

    def list_open_pull_numbers(self) -> list[int]:
        """Return the numbers of all open pull requests (paginated)."""
        out: list[int] = []
        page = 1
        while True:
            data = self._request(
                "GET",
                f"/repos/{self._owner}/{self._repo}/pulls?state=open&per_page=100&page={page}",
            )
            if not isinstance(data, list) or not data:
                break
            out.extend(int(item.get("number")) for item in data if item.get("number"))
            if len(data) < 100:
                break
            page += 1
        return out

    # ---- issues (used for the global 24h spend ledger) ------------------

    def find_issue_by_marker(self, marker: str) -> Optional[dict]:
        """Return the first open non-PR issue whose body contains ``marker``."""
        data = self._request(
            "GET",
            f"/repos/{self._owner}/{self._repo}/issues?state=open&per_page=100",
        )
        if not isinstance(data, list):
            return None
        for item in data:
            if item.get("pull_request"):  # PRs appear here too; skip them
                continue
            if marker in (item.get("body") or ""):
                return item
        return None

    def create_issue(self, title: str, body: str) -> dict:
        return self._request(
            "POST",
            f"/repos/{self._owner}/{self._repo}/issues",
            body={"title": title, "body": body},
        )

    def update_issue_body(self, issue_number: int, body: str) -> dict:
        return self._request(
            "PATCH",
            f"/repos/{self._owner}/{self._repo}/issues/{issue_number}",
            body={"body": body},
        )

    # NOTE: There is intentionally no merge_pr() method and no
    # delete_branch() method. Both require permissions the workflow
    # explicitly does not grant. See Section 10 and 11.5 of the design doc.


__all__ = ["GitHubAPI", "PRComment", "CheckRun", "GITHUB_API"]
