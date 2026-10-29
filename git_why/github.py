"""
github.py — fetch the discussion behind issue / PR references.

Design rules
------------
* **Standard library only** (``urllib``). No extra dependency for an optional feature.
* **Read-only, best effort.** Any failure degrades to "no GitHub evidence" plus
  a warning; it never breaks the local report.
* **Secrets.** The token comes from ``GITHUB_TOKEN`` (or ``GH_TOKEN``), is sent
  only in the ``Authorization`` header to the configured API host, and is never
  logged, printed, serialised or written to disk. There is no on-disk cache.
* **Budget.** At most ``max_requests`` HTTP calls per run, and we stop early on
  rate limiting or network errors instead of retrying.
* **Testable.** The HTTP transport is a plain callable that can be replaced.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import __version__

DEFAULT_API = "https://api.github.com"
USER_AGENT = f"git-why/{__version__} (+https://github.com/Dastan6752/git-why)"
MAX_EXCERPT = 400

# transport(url, headers, timeout) -> (status, headers, body)
Transport = Callable[[str, dict[str, str], float], tuple[int, dict[str, str], bytes]]


@dataclass
class Comment:
    author: str
    body: str
    url: str
    kind: str  # "review", "comment", "review_summary"
    path: str | None = None  # file a review comment was attached to


@dataclass
class Thread:
    """An issue or pull request with the evidence we gathered about it."""

    number: int
    kind: str  # "issue" or "pr"
    title: str
    state: str  # open / closed / merged
    url: str
    author: str
    body: str = ""
    labels: list[str] = field(default_factory=list)
    linked_issues: list[int] = field(default_factory=list)
    merged_by: str | None = None
    comments: list[Comment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "kind": self.kind,
            "title": self.title,
            "state": self.state,
            "url": self.url,
            "author": self.author,
            "body_excerpt": self.body,
            "labels": self.labels,
            "linked_issues": self.linked_issues,
            "merged_by": self.merged_by,
            "comments": [
                {"author": c.author, "kind": c.kind, "path": c.path, "url": c.url, "body_excerpt": c.body}
                for c in self.comments
            ],
        }


@dataclass
class GitHubStatus:
    enabled: bool = False
    repo: str | None = None
    authenticated: bool = False
    requests_made: int = 0
    rate_limited: bool = False
    offline: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "repo": self.repo,
            "authenticated": self.authenticated,
            "requests_made": self.requests_made,
            "rate_limited": self.rate_limited,
            "offline": self.offline,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Remote URL parsing
# ---------------------------------------------------------------------------

_REMOTE_PATTERNS = [
    re.compile(r"^https?://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"),
    re.compile(
        r"^(?:ssh://)?(?:[\w.-]+@)?(?P<host>[\w.-]+)(?::\d+)?[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
    ),
]


def parse_remote(url: str | None) -> tuple[str, str, str] | None:
    """Return (host, owner, repo) for GitHub-style remote URLs, else None."""
    if not url:
        return None
    for pattern in _REMOTE_PATTERNS:
        m = pattern.match(url.strip())
        if m:
            owner, repo = m["owner"], m["repo"]
            if owner and repo and "/" not in owner:
                return m["host"].lower(), owner, repo
    return None


def api_base_for(host: str) -> str:
    """API root for a host: github.com -> api.github.com, GHES -> https://host/api/v3."""
    env = os.environ.get("GITHUB_API_URL")
    if env:
        return env.rstrip("/")
    if host == "github.com":
        return DEFAULT_API
    return f"https://{host}/api/v3"


def token_from_env() -> str | None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def urllib_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, str], bytes]:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-HTTPS GitHub API URL: {url.split('://', 1)[0]}://…")
    req = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310 - scheme checked above
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body


class GitHubUnavailableError(Exception):
    """Raised internally to stop making requests (offline / rate limited)."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GitHubClient:
    def __init__(
        self,
        owner: str,
        repo: str,
        *,
        token: str | None = None,
        api_base: str = DEFAULT_API,
        transport: Transport | None = None,
        timeout: float = 10.0,
        max_requests: int = 40,
        max_comments: int = 5,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self._token = token
        self.api_base = api_base.rstrip("/")
        self._transport: Transport = transport or urllib_transport
        self.timeout = timeout
        self.max_requests = max_requests
        self.max_comments = max_comments
        self.status = GitHubStatus(enabled=True, repo=f"{owner}/{repo}", authenticated=bool(token))
        self._cache: dict[str, Any] = {}

    # -- low level -------------------------------------------------------------

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """GET JSON. Returns None for 404. Raises GitHubUnavailableError when we must stop."""
        if self.status.offline or self.status.rate_limited:
            raise GitHubUnavailableError
        url = f"{self.api_base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        if url in self._cache:
            return self._cache[url]
        if self.status.requests_made >= self.max_requests:
            self._warn_once(f"request budget of {self.max_requests} reached; some evidence not fetched")
            raise GitHubUnavailableError

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        self.status.requests_made += 1
        try:
            status, resp_headers, body = self._transport(url, headers, self.timeout)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            self.status.offline = True
            self._warn_once(f"GitHub unreachable ({type(exc).__name__}); continuing offline")
            raise GitHubUnavailableError from exc

        if status == 404:
            self._cache[url] = None
            return None
        if status in (401,):
            self.status.offline = True
            self._warn_once("GitHub rejected the token (401); check GITHUB_TOKEN")
            raise GitHubUnavailableError
        if status in (403, 429):
            remaining = resp_headers.get("x-ratelimit-remaining")
            if remaining == "0" or status == 429 or "rate limit" in body[:300].decode("utf-8", "replace").lower():
                self.status.rate_limited = True
                hint = "" if self._token else " (set GITHUB_TOKEN for a higher limit)"
                self._warn_once(f"GitHub API rate limit reached{hint}")
            else:
                self.status.offline = True
                self._warn_once("GitHub returned 403 (private repository or insufficient token scope?)")
            raise GitHubUnavailableError
        if status >= 500:
            self.status.offline = True
            self._warn_once(f"GitHub API error {status}; continuing offline")
            raise GitHubUnavailableError
        if status != 200:
            self._warn_once(f"unexpected GitHub response {status} for {path}")
            self._cache[url] = None
            return None
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._warn_once(f"could not parse GitHub response for {path}")
            self._cache[url] = None
            return None
        self._cache[url] = data
        return data

    def _warn_once(self, message: str) -> None:
        if message not in self.status.warnings:
            self.status.warnings.append(message)

    # -- high level --------------------------------------------------------------

    def thread(self, number: int) -> Thread | None:
        """Issue or PR ``number`` with its discussion, or None if unavailable."""
        try:
            data = self._get(f"/repos/{self.owner}/{self.repo}/issues/{number}")
        except GitHubUnavailableError:
            return None
        if not isinstance(data, dict):
            return None
        is_pr = "pull_request" in data
        thread = Thread(
            number=number,
            kind="pr" if is_pr else "issue",
            title=str(data.get("title") or "").strip(),
            state=str(data.get("state") or "unknown"),
            url=str(data.get("html_url") or f"https://github.com/{self.owner}/{self.repo}/issues/{number}"),
            author=_login(data.get("user")),
            body=excerpt(data.get("body")),
            labels=[
                str(lbl.get("name")) for lbl in data.get("labels") or [] if isinstance(lbl, dict) and lbl.get("name")
            ],
            linked_issues=linked_issue_numbers(data.get("body")),
        )
        if is_pr:
            self._enrich_pr(thread)
        self._add_comments(thread)
        return thread

    def pulls_for_commit(self, sha: str) -> list[int]:
        """PR numbers associated with a commit (GitHub's own association)."""
        try:
            data = self._get(f"/repos/{self.owner}/{self.repo}/commits/{sha}/pulls")
        except GitHubUnavailableError:
            return []
        if not isinstance(data, list):
            return []
        return [int(pr["number"]) for pr in data if isinstance(pr, dict) and isinstance(pr.get("number"), int)]

    def commit_url(self, sha: str) -> str:
        host = self.api_base.replace("https://api.github.com", "https://github.com")
        if host != "https://github.com":
            host = re.sub(r"/api/v3$", "", host)
        return f"{host}/{self.owner}/{self.repo}/commit/{sha}"

    # -- internals ---------------------------------------------------------------

    def _enrich_pr(self, thread: Thread) -> None:
        try:
            pr = self._get(f"/repos/{self.owner}/{self.repo}/pulls/{thread.number}")
        except GitHubUnavailableError:
            return
        if not isinstance(pr, dict):
            return
        if pr.get("merged_at") or pr.get("merged"):
            thread.state = "merged"
            thread.merged_by = _login(pr.get("merged_by")) or None

    def _add_comments(self, thread: Thread) -> None:
        budget = self.max_comments
        sources: list[tuple[str, str, dict[str, str]]] = []
        if thread.kind == "pr":
            sources.append(
                ("review", f"/repos/{self.owner}/{self.repo}/pulls/{thread.number}/comments", {"per_page": "30"})
            )
            sources.append(
                ("review_summary", f"/repos/{self.owner}/{self.repo}/pulls/{thread.number}/reviews", {"per_page": "30"})
            )
        sources.append(
            ("comment", f"/repos/{self.owner}/{self.repo}/issues/{thread.number}/comments", {"per_page": "30"})
        )

        for kind, path, params in sources:
            if budget <= 0:
                return
            try:
                items = self._get(path, params)
            except GitHubUnavailableError:
                return
            if not isinstance(items, list):
                continue
            for item in items:
                if budget <= 0:
                    break
                if not isinstance(item, dict):
                    continue
                body = excerpt(item.get("body"))
                if not body or _is_bot(item.get("user")):
                    continue
                thread.comments.append(
                    Comment(
                        author=_login(item.get("user")),
                        body=body,
                        url=str(item.get("html_url") or thread.url),
                        kind=kind,
                        path=item.get("path") if kind == "review" else None,
                    )
                )
                budget -= 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_CLOSING_IN_BODY = re.compile(
    r"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\b:?\s+(?:[\w.-]+/[\w.-]+)?#(\d+)", re.IGNORECASE
)


def linked_issue_numbers(body: object) -> list[int]:
    if not isinstance(body, str):
        return []
    seen: list[int] = []
    for num in _CLOSING_IN_BODY.findall(body):
        n = int(num)
        if n not in seen:
            seen.append(n)
    return seen


def excerpt(text: object, limit: int = MAX_EXCERPT) -> str:
    if not isinstance(text, str):
        return ""
    cleaned = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)  # strip PR-template comments
    cleaned = re.sub(r"\r\n?", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def _login(user: object) -> str:
    if isinstance(user, dict):
        return str(user.get("login") or "")
    return ""


def _is_bot(user: object) -> bool:
    return isinstance(user, dict) and (user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]"))
