"""GitHub integration, fully mocked — no network, no credentials."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from git_why import github
from git_why.core import Options, investigate
from git_why.github import GitHubClient, api_base_for, excerpt, linked_issue_numbers, parse_remote

from tests.conftest import git

# ---------- remote parsing ----------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/psf/requests.git", ("github.com", "psf", "requests")),
        ("https://github.com/psf/requests", ("github.com", "psf", "requests")),
        ("https://user:tok@github.com/psf/requests/", ("github.com", "psf", "requests")),
        ("git@github.com:psf/requests.git", ("github.com", "psf", "requests")),
        ("ssh://git@github.com/psf/requests.git", ("github.com", "psf", "requests")),
        ("ssh://git@ghe.corp.example:2222/team/app.git", ("ghe.corp.example", "team", "app")),
        ("https://gitlab.com/group/sub/project.git", None),  # nested groups: not owner/name
        ("", None),
        (None, None),
    ],
)
def test_parse_remote(url: str | None, expected: tuple[str, str, str] | None) -> None:
    assert parse_remote(url) == expected


def test_api_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    assert api_base_for("github.com") == "https://api.github.com"
    assert api_base_for("ghe.corp.example") == "https://ghe.corp.example/api/v3"
    monkeypatch.setenv("GITHUB_API_URL", "https://proxy.local/api/")
    assert api_base_for("github.com") == "https://proxy.local/api"


def test_helpers() -> None:
    assert linked_issue_numbers("Fixes #12, closes org/repo#13 and #12 again") == [12, 13]
    assert linked_issue_numbers(None) == []
    assert excerpt("<!-- template -->\n\nHello\n\n\n\nWorld", 100) == "Hello\n\nWorld"
    assert excerpt("x" * 500, 50).endswith("…") and len(excerpt("x" * 500, 50)) == 50
    assert excerpt(123) == ""


# ---------- fake transport ----------


class FakeTransport:
    """Scripted HTTP responses keyed by URL path; records requests and headers."""

    def __init__(self, routes: dict[str, Any], *, fail: Exception | None = None) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.fail = fail

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, str], bytes]:
        self.calls.append(url)
        self.headers.append(headers)
        if self.fail:
            raise self.fail
        path = url.split("api.github.com", 1)[1].split("?", 1)[0]
        route = self.routes.get(path)
        if route is None:
            return 404, {}, b'{"message":"Not Found"}'
        if isinstance(route, tuple):  # (status, headers, body)
            status, hdrs, body = route
            return status, hdrs, body if isinstance(body, bytes) else json.dumps(body).encode()
        return 200, {}, json.dumps(route).encode()


ROUTES: dict[str, Any] = {
    "/repos/o/r/issues/7": {
        "title": "Retries logic",
        "state": "closed",
        "html_url": "https://github.com/o/r/pull/7",
        "user": {"login": "sigmavirus24"},
        "body": "<!-- tpl -->Fixes #42\n\nMake retries consistent.",
        "labels": [{"name": "bug"}],
        "pull_request": {"url": "..."},
    },
    "/repos/o/r/pulls/7": {"merged_at": "2014-12-01T00:00:00Z", "merged_by": {"login": "kennethreitz"}},
    "/repos/o/r/pulls/7/comments": [
        {
            "user": {"login": "kevinburke"},
            "body": "doesn't this mean only ints?",
            "html_url": "u1",
            "path": "adapters.py",
        },
        {"user": {"login": "dependabot[bot]"}, "body": "bot noise", "html_url": "u2"},
    ],
    "/repos/o/r/pulls/7/reviews": [{"user": {"login": "rev"}, "body": "", "html_url": "u3"}],
    "/repos/o/r/issues/7/comments": [
        {"user": {"login": "sigmavirus24"}, "body": "0 is a magic number", "html_url": "u4"}
    ],
    "/repos/o/r/issues/42": {
        "title": "Double charge under load",
        "state": "closed",
        "html_url": "https://github.com/o/r/issues/42",
        "user": {"login": "reporter"},
        "body": "Webhook retried instantly.",
        "labels": [],
    },
    "/repos/o/r/issues/42/comments": [],
    "/repos/o/r/commits/abc/pulls": [{"number": 7}],
}


def test_thread_for_pr_collects_discussion() -> None:
    t = FakeTransport(ROUTES)
    client = GitHubClient("o", "r", token="secret-token", transport=t)
    thread = client.thread(7)
    assert thread is not None
    assert thread.kind == "pr" and thread.state == "merged" and thread.merged_by == "kennethreitz"
    assert thread.title == "Retries logic" and thread.labels == ["bug"]
    assert thread.linked_issues == [42]
    assert thread.body.startswith("Fixes #42")
    kinds = [(c.author, c.kind) for c in thread.comments]
    assert ("kevinburke", "review") in kinds and ("sigmavirus24", "comment") in kinds
    assert not any("bot" in a for a, _ in kinds)  # bots filtered, empty reviews skipped
    # auth header sent, token never in status/serialisation
    assert t.headers[0]["Authorization"] == "Bearer secret-token"
    assert client.status.authenticated is True
    assert "secret-token" not in json.dumps(client.status.to_dict())
    assert "secret-token" not in json.dumps(thread.to_dict())


def test_thread_for_issue_and_cache() -> None:
    t = FakeTransport(ROUTES)
    client = GitHubClient("o", "r", transport=t)
    a = client.thread(42)
    b = client.thread(42)
    assert a is not None and a.kind == "issue" and a.author == "reporter"
    assert b is not None
    assert len([c for c in t.calls if c.endswith("/issues/42")]) == 1  # cached
    assert "Authorization" not in t.headers[0]


def test_missing_thread_returns_none() -> None:
    client = GitHubClient("o", "r", transport=FakeTransport(ROUTES))
    assert client.thread(999) is None
    assert client.pulls_for_commit("nope") == []
    assert client.pulls_for_commit("abc") == [7]


def test_rate_limit_stops_further_requests() -> None:
    routes = {"/repos/o/r/issues/7": (403, {"x-ratelimit-remaining": "0"}, {"message": "API rate limit exceeded"})}
    t = FakeTransport(routes)
    client = GitHubClient("o", "r", transport=t)
    assert client.thread(7) is None
    assert client.thread(42) is None
    assert client.status.rate_limited
    assert len(t.calls) == 1  # no further calls after the limit
    assert any("rate limit" in w for w in client.status.warnings)
    assert any("GITHUB_TOKEN" in w for w in client.status.warnings)


def test_forbidden_without_rate_limit_is_reported_as_access_problem() -> None:
    routes = {"/repos/o/r/issues/7": (403, {"x-ratelimit-remaining": "40"}, {"message": "Resource not accessible"})}
    client = GitHubClient("o", "r", transport=FakeTransport(routes))
    assert client.thread(7) is None
    assert client.status.offline and any("private" in w for w in client.status.warnings)


def test_bad_token() -> None:
    routes: dict[str, Any] = {"/repos/o/r/issues/7": (401, {}, {"message": "Bad credentials"})}
    client = GitHubClient("o", "r", token="bad", transport=FakeTransport(routes))
    assert client.thread(7) is None
    assert any("401" in w for w in client.status.warnings)
    assert "bad" not in " ".join(client.status.warnings).replace("Bad credentials", "")


def test_network_failure_goes_offline() -> None:
    t = FakeTransport({}, fail=OSError("no route to host"))
    client = GitHubClient("o", "r", transport=t)
    assert client.thread(7) is None
    assert client.thread(8) is None
    assert client.status.offline and len(t.calls) == 1


def test_server_error_and_bad_json() -> None:
    routes: dict[str, Any] = {
        "/repos/o/r/issues/1": (500, {}, b"boom"),
    }
    client = GitHubClient("o", "r", transport=FakeTransport(routes))
    assert client.thread(1) is None and client.status.offline

    routes = {"/repos/o/r/issues/2": (200, {}, b"not json")}
    client = GitHubClient("o", "r", transport=FakeTransport(routes))
    assert client.thread(2) is None
    assert any("parse" in w for w in client.status.warnings)


def test_incomplete_response_is_tolerated() -> None:
    routes: dict[str, Any] = {"/repos/o/r/issues/3": {"title": None, "user": None, "labels": "weird", "body": 5}}
    client = GitHubClient("o", "r", transport=FakeTransport(routes))
    thread = client.thread(3)
    assert thread is not None and thread.title == "" and thread.author == "" and thread.labels == []


def test_request_budget() -> None:
    t = FakeTransport(ROUTES)
    client = GitHubClient("o", "r", transport=t, max_requests=2)
    client.thread(7)
    assert len(t.calls) == 2
    assert any("budget" in w for w in client.status.warnings)


def test_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert github.token_from_env() is None
    monkeypatch.setenv("GH_TOKEN", "  t2 ")
    assert github.token_from_env() == "t2"
    monkeypatch.setenv("GITHUB_TOKEN", "t1")
    assert github.token_from_env() == "t1"


# ---------- end to end with a repo ----------


def test_investigate_with_github_attaches_threads(blind_spot_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git(blind_spot_repo, "remote", "add", "origin", "git@github.com:o/r.git")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    transport = FakeTransport(ROUTES)

    def factory(owner: str, repo: str, **kw: Any) -> GitHubClient:
        return GitHubClient(owner, repo, transport=transport, **kw)

    report = investigate(blind_spot_repo / "cfg.py", 2, 2, Options(github=True, github_client_factory=factory))
    assert report.github.enabled and report.github.repo == "o/r" and report.github.authenticated
    assert 7 in report.threads and 42 in report.threads
    b = next(s for s in report.steps if s.subject.startswith("B"))
    assert {t.number for t in report.threads_for(b)} == {7, 42}
    assert report.evidence_strength(b) == "strong"
    assert report.web_base == "https://github.com/o/r"
    assert report.commit_url(b.sha) == f"https://github.com/o/r/commit/{b.sha}"
    dumped = json.dumps(report.to_dict())
    assert "tok" not in dumped.replace("token", "")  # no secret leaks
    assert "Retries logic" in dumped


def test_investigate_with_github_but_no_github_remote(blind_spot_repo: Path) -> None:
    report = investigate(blind_spot_repo / "cfg.py", 2, 2, Options(github=True))
    assert report.github.enabled
    assert any("not a GitHub remote" in w for w in report.github.warnings)
    assert report.threads == {}


def test_investigate_offline_github_keeps_local_evidence(blind_spot_repo: Path) -> None:
    git(blind_spot_repo, "remote", "add", "origin", "https://github.com/o/r.git")

    def factory(owner: str, repo: str, **kw: Any) -> GitHubClient:
        return GitHubClient(owner, repo, transport=FakeTransport({}, fail=OSError("offline")), **kw)

    report = investigate(blind_spot_repo / "cfg.py", 2, 2, Options(github=True, github_client_factory=factory))
    assert len(report.steps) == 3
    assert report.github.offline
    assert any("unreachable" in w for w in report.github.warnings)
