"""Noise classification rules and the rich/plain renderers (with GitHub threads)."""

from __future__ import annotations

import io
import json
from pathlib import Path

from git_why import noise
from git_why.github import Comment, GitHubStatus, Thread
from git_why.history import Hunk, RepoInfo, Step
from git_why.merges import _relevant, _signature, order_newest_first
from git_why.render import render_json, render_plain, render_rich
from git_why.report import Report, Target

from tests.conftest import write_commit


def mk(sha: str, ts: int = 1, subject: str = "s", **kw: object) -> Step:
    s = Step(sha * 40, sha * 7, "ann", "a@x", "2024-01-01T00:00:00+00:00", ts, subject, str(kw.get("body", "")))
    s.file_before = kw.get("file_before", "a.py")  # type: ignore[assignment]
    s.file_after = kw.get("file_after", "a.py")  # type: ignore[assignment]
    s.hunks = [Hunk(removed=list(kw.get("removed", [])), added=list(kw.get("added", [])))]  # type: ignore[call-overload]
    if "parents" in kw:
        s.parents = list(kw["parents"])  # type: ignore[call-overload]
    return s


# ---------- noise ----------


def test_noise_blank_lines_only_even_if_it_looks_like_an_origin() -> None:
    s = noise.classify(mk("a", added=["", "   "]))
    assert s.is_noise and s.noise_reason == "blank lines only"


def test_noise_pure_move_between_files() -> None:
    s = mk("a", file_before="old/a.py", file_after="new/a.py", removed=["x = 1"], added=["x = 1"])
    s = noise.classify(s)
    assert s.is_noise and "formatting" in s.noise_reason  # identical text is caught by the whitespace rule first


def test_noise_style_commit_with_tiny_change() -> None:
    s = mk(
        "a",
        subject="style: prettier",
        removed=["logger.info('starting worker')"],
        added=['logger.info("starting worker")'],
    )
    assert noise.classify(s).is_noise
    assert noise.classify(s).noise_reason == "looks like a style-only change"


def test_noise_style_subject_but_real_change_is_kept() -> None:
    s = mk("a", subject="style: also change timeout", removed=["t = 1"], added=["t = 100000"])
    assert not noise.classify(s).is_noise


def test_noise_respects_prior_decision() -> None:
    s = mk("a", removed=["x = 1"], added=["x = 2"])
    s.is_noise, s.noise_reason = True, "moved within file"
    assert noise.classify(s).noise_reason == "moved within file"


# ---------- merges helpers ----------


def test_relevant_filters_conflict_markers_and_lineage() -> None:
    s = mk("m", removed=["<<<<<<< a", "T = 1", "=======", "T = 2", ">>>>>>> b", "other = 9"], added=["T = 3"])
    removed, added = _relevant(s, {"T = 1", "T = 2", "T = 3"})
    assert removed == ["T = 1", "T = 2"] and added == ["T = 3"]
    # unknown lineage -> everything except markers
    removed, added = _relevant(s, {"Z = 0"})
    assert removed == ["T = 1", "T = 2", "other = 9"] and added == ["T = 3"]
    assert _signature(s) == (("<<<<<<< a", "=======", ">>>>>>> b", "T = 1", "T = 2", "other = 9"), ("T = 3",))


def test_order_newest_first_breaks_ties_topologically(make_repo) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo()
    import os

    from tests.conftest import GIT_ENV, git

    # three commits in the same second
    env = {**GIT_ENV, "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000"}
    shas = []
    for i in range(3):
        (repo / "f.py").write_text(f"v = {i}\n")
        import subprocess

        subprocess.run(["git", "add", "f.py"], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", f"c{i}"], cwd=repo, check=True, env=env)
        shas.append(git(repo, "rev-parse", "HEAD").strip())
    assert os.path.exists(repo / ".git")
    steps = [Step(s, s[:7], "a", "a@x", "d", 1700000000, f"c{i}", "") for i, s in enumerate(shas)]
    ordered = order_newest_first(repo, [steps[0], steps[2], steps[1]])
    assert [s.subject for s in ordered] == ["c2", "c1", "c0"]


# ---------- renderers ----------


def sample_report(tmp_path: Path) -> Report:
    origin = mk("a", 1, "add retry\n", body="Fixes #42: double charges", added=["sleep(1.0)"])
    origin.file_before = None
    fmt = mk("b", 2, "style: fmt", removed=["sleep(1.0)"], added=["sleep( 1.0 )"])
    fmt.is_noise, fmt.noise_reason = True, "whitespace / formatting only"
    change = mk("c", 3, "tune (#99)", removed=["sleep(1.0)"], added=["sleep(0.3)"], body="p99 regressed")
    change.found_by = "pickaxe"
    change.committed = "2024-03-15T12:00:00+00:00"  # rebased: commit day differs from author day
    change.landed_via = mk("d", 4, "Merge pull request #99 from x/y")
    merge = mk(
        "e", 5, "resolve conflict", removed=["sleep(0.3)", "sleep(0.5)"], added=["sleep(0.4)"], parents=["1", "2"]
    )
    deletion = mk("f", 6, "drop line", removed=["sleep(0.4)"])
    reborn = mk("g", 7, "bring back", added=["sleep(0.4)"])
    reborn.shallow_boundary = False
    steps = [reborn, deletion, merge, change, fmt, origin]
    report = Report(
        target=Target("f.py", 3, 3, ["sleep(0.4)"]),
        repo=RepoInfo(tmp_path, "h" * 40, False, set(), "git@github.com:o/r.git"),
        steps=steps,
        pickaxe_used=True,
        lineage_complete=True,
        merges_scanned=True,
        warnings=["f.py has uncommitted changes; line numbers refer to HEAD"],
        web_base="https://github.com/o/r",
    )
    report.github = GitHubStatus(enabled=True, repo="o/r", warnings=["GitHub API rate limit reached"])
    report.threads[99] = Thread(
        99,
        "pr",
        "Tune the backoff",
        "merged",
        "https://github.com/o/r/pull/99",
        "sara",
        body="<!-- x -->\n\nWe measured p99.\n\n\nSee dashboard.",
        linked_issues=[42],
        comments=[Comment("rev", "0.3 was tuned empirically", "u", "review", path="f.py")],
    )
    report.threads[42] = Thread(42, "issue", "Double charge", "closed", "https://github.com/o/r/issues/42", "bob")
    return report


def test_render_rich_covers_all_step_kinds(tmp_path: Path) -> None:
    buf = io.StringIO()
    render_rich(sample_report(tmp_path), color=False, out=buf)
    out = buf.getvalue()
    assert "\x1b[" not in out
    for needle in (
        "line created here",
        "line re-created here",
        "line deleted here",
        "changed by the merge itself",
        "invisible to git blame",
        "skipped",
        "landed via merge",
        "PR #99 Tune the backoff (merged, @sara)",
        "@rev (review on f.py): 0.3 was tuned empirically",
        "links: #42",
        "issue #42 Double charge",
        "evidence: strong",
        "warning: f.py has uncommitted changes",
        "warning: GitHub API rate limit reached",
        "Confidence:",
        "https://github.com/o/r/commit/",
    ):
        assert needle in out, needle
    # a thread referenced twice is detailed once
    assert out.count("https://github.com/o/r/pull/99") == 1


def test_render_plain_covers_all_step_kinds(tmp_path: Path) -> None:
    buf = io.StringIO()
    render_plain(sample_report(tmp_path), out=buf)
    out = buf.getvalue()
    assert out.isascii()
    for needle in (
        "x 2024-01-01 fffffff",
        "line deleted here",
        "[changed by the merge itself]",
        "[invisible to git blame]",
        "PR #99: Tune the backoff (merged, @sara)",
        "@rev (review): 0.3 was tuned empirically",
        "issue #42: Double charge",
        "-> PR #99",
        "warning: GitHub API rate limit reached",
        "* 2024-01-01 (committed 2024-03-15) ccccccc",
    ):
        assert needle in out, needle
    assert out.count("(committed") == 1  # only shown when it differs from the author date


def test_render_json_roundtrip(tmp_path: Path) -> None:
    buf = io.StringIO()
    render_json(sample_report(tmp_path), out=buf)
    data = json.loads(buf.getvalue())
    kinds = [s["kind"] for s in data["steps"]]
    assert kinds == ["origin", "noise", "change", "change", "deletion", "origin"]
    assert data["steps"][2]["github_threads"][0]["number"] == 99
    assert data["steps"][3]["is_merge"] is True
    assert data["steps"][2]["committed"] == "2024-03-15T12:00:00+00:00"
    assert data["steps"][0]["committed"] is None
    assert data["github"]["warnings"] == ["GitHub API rate limit reached"]


def test_render_long_bodies_and_hunks_are_truncated(make_repo) -> None:  # type: ignore[no-untyped-def]
    from git_why.core import Options, investigate

    repo = make_repo()
    body = "\n".join(f"reason line {i}" for i in range(20))
    write_commit(repo, "big.py", "".join(f"v{i} = {i}\n" for i in range(30)), f"A: big\n\n{body}")
    write_commit(repo, "big.py", "".join(f"v{i} = {i + 1}\n" for i in range(30)), "B: bump all")
    report = investigate(repo / "big.py", 1, 30, Options())
    buf = io.StringIO()
    render_rich(report, color=False, max_body=3, out=buf)
    out = buf.getvalue()
    assert "… (17 more lines)" in out
    assert "removed, 30 added in total" in out
    assert "… 22 more lines" in out


def test_render_json_is_safe_for_cp1251_stream(tmp_path: Path) -> None:
    report = sample_report(tmp_path)
    report.steps[0].subject = "contains emoji 🪦"

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1251")
    render_json(report, out=stream)
    stream.flush()

    serialized = raw.getvalue().decode("cp1251")
    assert serialized.isascii()

    data = json.loads(serialized)
    assert data["schema_version"] == 1
    assert "🪦" in json.dumps(data, ensure_ascii=False)
