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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .redact import redact


GITHUB_API = "https://api.github.com"

# Statuses safe to retry: the request was rejected BEFORE GitHub processed it,
# so retrying cannot double-apply a write (e.g. double-post a comment). 429 =
# rate limited / secondary limit, 503 = service unavailable. A primary
# rate-limit also arrives as 403 with rate-limit headers (handled separately).
# 500/502/504 are deliberately NOT retried — they may have been partially
# processed, and a blind retry of a POST could duplicate a comment.
_RETRYABLE_STATUS = frozenset({429, 503})
_MAX_ATTEMPTS = 4
_BASE_DELAY = 1.0
_MAX_DELAY = 30.0  # cap every wait so a job never hangs near the Actions timeout


def _sleep(seconds: float) -> None:
    """Indirection so tests can monkeypatch the backoff wait to a no-op."""
    time.sleep(seconds)


def _is_rate_limited_403(exc: urllib.error.HTTPError, detail: str) -> bool:
    """A 403 that is really a rate limit (vs a genuine permission denial)."""
    if exc.code != 403:
        return False
    headers = exc.headers or {}
    if (headers.get("X-RateLimit-Remaining") or "").strip() == "0":
        return True
    if headers.get("Retry-After"):
        return True
    low = (detail or "").lower()
    return "rate limit" in low or "secondary rate" in low


def _retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
    """Seconds to wait before a retry, honoring Retry-After / reset, capped."""
    headers = exc.headers or {}
    ra = headers.get("Retry-After")
    if ra:
        try:
            return min(float(ra), _MAX_DELAY)
        except (TypeError, ValueError):
            pass
    reset = headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return min(max(float(reset) - time.time(), 0.0), _MAX_DELAY)
        except (TypeError, ValueError):
            pass
    return min(_BASE_DELAY * (2 ** (attempt - 1)), _MAX_DELAY)


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
        # marker -> issue number, learned once per run so repeated ledger
        # lookups fetch the issue directly instead of re-listing every issue.
        self._marker_issue_cache: dict[str, int] = {}

    @classmethod
    def from_app(
        cls,
        *,
        app_id: object,
        private_key_pem: str,
        owner: str,
        repo: str,
        app: object = None,
    ) -> "GitHubAPI":
        """Build a client authed with a GitHub App *installation* token.

        Each installation has its own 5–15k req/hr quota, so this lifts the
        per-repo GITHUB_TOKEN 1k/hr cap and stops every tenant sharing one PAT
        bucket (see github_app.py / SCALING.md Move 1). The token is minted once
        here; for a CI run it comfortably outlives the job.
        """
        from .github_app import GitHubApp  # optional path; keep the import local
        minter = app or GitHubApp(app_id, private_key_pem)
        return cls(minter.token_for_repo(owner, repo), owner, repo)

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
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
            "User-Agent": "TradeWatcher-Dispatcher/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }
        # Retry transient rate limits (429 / rate-limit 403 / 503) with capped
        # backoff so a momentary limit no longer fails a whole round. Writes are
        # only retried when GitHub rejected them before processing (see
        # _RETRYABLE_STATUS), so a comment is never double-posted.
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload_bytes = resp.read()
                if raw:
                    return payload_bytes.decode("utf-8", errors="replace")
                return json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
                retryable = exc.code in _RETRYABLE_STATUS or _is_rate_limited_403(exc, detail)
                if retryable and attempt < _MAX_ATTEMPTS:
                    _sleep(_retry_delay(exc, attempt))
                    continue
                raise RuntimeError(
                    f"GitHub API {method} {path} HTTP {exc.code}: {detail[:500]}"
                ) from exc
            except urllib.error.URLError as exc:
                # A network error on a GET is safe to retry; a write may have
                # been applied server-side, so non-GETs are not blindly retried.
                if method == "GET" and attempt < _MAX_ATTEMPTS:
                    _sleep(min(_BASE_DELAY * (2 ** (attempt - 1)), _MAX_DELAY))
                    continue
                raise RuntimeError(
                    f"GitHub API {method} {path} failed: {exc.reason}"
                ) from exc
        raise RuntimeError(f"GitHub API {method} {path}: exhausted retries")

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

    def list_open_pulls_with_labels(self) -> list[tuple[int, list[str]]]:
        """(number, label-names) for every open PR, in ONE paginated list call.

        The pulls list response already embeds each PR's labels, so callers that
        only need to filter by label (the scheduled sweep, the pause-all sweep)
        get them here instead of a per-PR ``list_labels`` round-trip — turning
        O(1 + N) API calls into O(1). This is the main lever against the
        per-repo rate limit on a busy repo.
        """
        out: list[tuple[int, list[str]]] = []
        page = 1
        while True:
            data = self._request(
                "GET",
                f"/repos/{self._owner}/{self._repo}/pulls?state=open&per_page=100&page={page}",
            )
            if not isinstance(data, list) or not data:
                break
            for item in data:
                num = item.get("number")
                if not num:
                    continue
                labels = [
                    l.get("name", "")
                    for l in (item.get("labels") or [])
                    if isinstance(l, dict)
                ]
                out.append((int(num), labels))
            if len(data) < 100:
                break
            page += 1
        return out

    def list_open_pull_numbers(self) -> list[int]:
        """Numbers of all open pull requests (derived from the labelled list)."""
        return [n for n, _labels in self.list_open_pulls_with_labels()]

    # ---- issues (used for the global 24h spend ledger) ------------------

    def find_issue_by_marker(self, marker: str) -> Optional[dict]:
        """Return the first open non-PR issue whose body contains ``marker``.

        The ledger lookup happens several times per round; the first call learns
        the issue number and caches it, so subsequent calls fetch that issue
        directly (one cheap GET, fresh body) instead of re-listing every open
        issue. Falls back to a full scan if the cached issue no longer matches.
        """
        cached = self._marker_issue_cache.get(marker)
        if cached is not None:
            try:
                issue = self._request(
                    "GET", f"/repos/{self._owner}/{self._repo}/issues/{cached}"
                )
            except RuntimeError:
                issue = None
            if isinstance(issue, dict) and marker in (issue.get("body") or ""):
                return issue
            self._marker_issue_cache.pop(marker, None)  # stale; rescan below

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
                num = item.get("number")
                if num:
                    self._marker_issue_cache[marker] = int(num)
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
