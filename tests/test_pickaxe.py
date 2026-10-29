"""
Tests for the pickaxe walk — including an integration test that builds a real
git repo reproducing git's merge blind spot, and proves we recover from it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from git_why import history, noise
from git_why.history import Hunk, Step
from git_why.pickaxe import (
    PickaxeWalker,
    anchored_regex,
    ere_escape,
    merge_timelines,
    worth_searching,
)

# ---------- pure unit tests ----------


def test_ere_escape_handles_regex_specials() -> None:
    assert ere_escape("time.sleep(0.3 * x)") == r"time\.sleep\(0\.3 \* x\)"
    assert ere_escape("a[0] + {b} | c?") == r"a\[0\] \+ \{b\} \| c\?"


def test_anchored_regex_ignores_surrounding_whitespace() -> None:
    assert anchored_regex("   x = 1  ") == "^[[:space:]]*x = 1[[:space:]]*$"


@pytest.mark.parametrize(
    "line,expected",
    [
        ("}", False),
        ("    pass", False),
        ("return", False),
        ("DEFAULT_RETRIES = 0", True),
        ("x=1", False),  # too little signal
        ("    time.sleep(0.3 * attempt)", True),
    ],
)
def test_worth_searching(line, expected):
    assert worth_searching(line) is expected


def _step(sha: str, ts: int, found_by: str = "range") -> Step:
    return Step(
        sha, sha[:7], "a", "a@x", "2024-01-01T00:00:00+00:00", ts, "s", "", hunks=[Hunk(added=["x"])], found_by=found_by
    )


def test_merge_timelines_dedups_and_sorts_newest_first() -> None:
    r = [_step("a" * 40, 100), _step("c" * 40, 300)]
    p = [_step("a" * 40, 100, "pickaxe"), _step("b" * 40, 200, "pickaxe")]
    merged = merge_timelines(r, p)
    assert [s.sha[0] for s in merged] == ["c", "b", "a"]
    # -L result wins on the shared commit
    assert merged[2].found_by == "range"


# ---------- integration test: reproduce git's blind spot ----------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def commit_file(repo: Path, content: str, msg: str) -> None:
    (repo / "cfg.py").write_text(content)
    git(repo, "add", "cfg.py")
    git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def blind_spot_repo(tmp_path: Path) -> Path:
    """
    main:     A(=0) ─────────────── M(merge, =0) ── D(unrelated)
                \\                  /
    feature:     B(=object()) ── C(=0)

    The line is `= 0` on both sides of the merge, so git's history
    simplification ignores the feature branch entirely.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit_file(repo, "import os\nRETRIES = 0\n", "A: base config")

    git(repo, "checkout", "-q", "-b", "feature")
    commit_file(repo, "import os\nRETRIES = object()\n", "B: use sentinel\n\nFixes #42")
    commit_file(repo, "import os\nRETRIES = 0\n", "C: revert to 0, sentinel broke docs\n\nSee PR #99")

    git(repo, "checkout", "-q", "main")
    # an unrelated mainline commit so the merge is a real merge, not fast-forward
    (repo / "other.txt").write_text("hi\n")
    git(repo, "add", "other.txt")
    git(repo, "commit", "-q", "-m", "mainline work")
    git(repo, "merge", "-q", "--no-ff", "-m", "M: merge feature", "feature")
    commit_file(repo, "import os\nRETRIES = 0\n# trailing comment\n", "D: unrelated edit")
    return repo


def test_git_log_L_alone_misses_side_branch(blind_spot_repo: Path) -> None:
    steps = history.line_history(blind_spot_repo, "cfg.py", 2, 2)
    subjects = [s.subject for s in steps]
    # This documents git's behaviour. If git ever fixes it, this test will
    # tell us and we can celebrate.
    assert subjects == ["A: base config"]


def test_pickaxe_walk_recovers_side_branch(blind_spot_repo: Path) -> None:
    walker = PickaxeWalker(blind_spot_repo)
    found = walker.walk("cfg.py", "RETRIES = 0")
    subjects = sorted(s.subject[:1] for s in found)
    assert subjects == ["A", "B", "C"]
    # It should have needed exactly two distinct searches: "= 0" and "= object()"
    assert walker.searches == 2


def test_combined_timeline_is_complete_and_ordered(blind_spot_repo: Path) -> None:
    range_steps = history.line_history(blind_spot_repo, "cfg.py", 2, 2)
    walker = PickaxeWalker(blind_spot_repo)
    walker.walk("cfg.py", "RETRIES = 0")
    steps = noise.classify_all(merge_timelines(range_steps, walker.found))

    oldest_first = [s.subject[:1] for s in reversed(steps)]
    assert oldest_first == ["A", "B", "C"]

    a, b, c = reversed(steps)
    assert a.is_origin and a.found_by == "range"
    assert b.found_by == "pickaxe" and b.removed == ["RETRIES = 0"] and b.added == ["RETRIES = object()"]
    assert c.found_by == "pickaxe" and c.removed == ["RETRIES = object()"] and c.added == ["RETRIES = 0"]
    assert not any(s.is_noise for s in steps)


def test_pickaxe_follows_renames(blind_spot_repo: Path) -> None:
    repo = blind_spot_repo
    git(repo, "mv", "cfg.py", "settings.py")
    git(repo, "commit", "-q", "-m", "rename cfg -> settings")
    walker = PickaxeWalker(repo)
    found = walker.walk("settings.py", "RETRIES = 0")
    assert sorted(s.subject[:1] for s in found) == ["A", "B", "C"]


# ---------- range drift & ambiguity ----------


def test_trim_range_drift_drops_unrelated_L_steps() -> None:
    """Simulates git log -L widening its range onto a neighbouring __version__ line."""
    lineage = {'__title__ = "requests"', "__title__ = 'requests'"}
    fmt = _step("f" * 40, 300)
    fmt.hunks = [Hunk(removed=["__title__ = 'requests'", "__version__ = '2.27.1'"], added=['__title__ = "requests"'])]
    bump = _step("b" * 40, 200)
    bump.hunks = [Hunk(removed=["__version__ = '2.27.0'"], added=["__version__ = '2.27.1'"])]
    birth = _step("a" * 40, 100)
    birth.hunks = [Hunk(added=["__title__ = 'requests'", "__version__ = '2.14.2'"])]

    origin = _step("a" * 40, 100, "pickaxe")  # the walk reached the birth => lineage complete
    merged = merge_timelines([fmt, bump, birth], [origin], lineage)

    assert [s.sha[0] for s in merged] == ["f", "a"]  # version bump dropped
    assert merged[0].removed == ["__title__ = 'requests'"]  # hunk focused on our line
    assert merged[1].added == ["__title__ = 'requests'"]


def test_trim_keeps_older_steps_when_lineage_incomplete() -> None:
    lineage = {"x = 2"}
    older = _step("a" * 40, 100)
    older.hunks = [Hunk(removed=["x = 0"], added=["x = 1"])]  # not in lineage
    pick = _step("c" * 40, 300, "pickaxe")
    pick.hunks = [Hunk(removed=["x = 1"], added=["x = 2"])]  # walk stopped here (no origin)
    merged = merge_timelines([older], [pick], lineage)
    assert [s.sha[0] for s in merged] == ["c", "a"]  # older step survived


def test_no_trim_without_pickaxe_results() -> None:
    step = _step("a" * 40, 100)
    step.hunks = [Hunk(removed=["old"], added=["new"])]
    assert merge_timelines([step], [], {"new"}) == [step]


def test_ambiguous_text_is_not_walked(tmp_path: Path) -> None:
    """Two identical lines in one file: the pickaxe can't tell which is ours."""
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit_file(repo, "a = 1\nTIMEOUT = 30\n", "first")
    commit_file(repo, "a = 1\nTIMEOUT = 30\nb = 2\nTIMEOUT = 30\n", "second copy")
    content = (repo / "cfg.py").read_text()
    walker = PickaxeWalker(repo)
    walker.walk("cfg.py", "TIMEOUT = 30", content)
    assert walker.searches == 0
    assert walker.lineage == {"TIMEOUT = 30"}  # still recorded for -L trimming


def test_pickaxe_finds_deletion_on_side_branch_after_rename(make_repo) -> None:  # type: ignore[no-untyped-def]
    """
    main:  A(line) ── M(merge, base == main: line gone) ── R(re-add) ── rename to src/
              \\── B(deletes line) ──┘
    """
    from tests.conftest import git, write_commit

    repo = make_repo("follow-gap")
    gap = "\n" * 6
    write_commit(repo, "v.py", f"__title__ = 'x'{gap}__version__ = '1'\n", "A: add")
    git(repo, "checkout", "-q", "-b", "side")
    write_commit(repo, "v.py", f"{gap}__version__ = '1'\n", "B: drop title")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "M: merge side", "side")
    write_commit(repo, "v.py", f"__title__ = 'x'{gap}__version__ = '1'\n", "R: re-add title")
    (repo / "src").mkdir()
    git(repo, "mv", "v.py", "src/v.py")
    git(repo, "commit", "-q", "-m", "move to src/")

    walker = PickaxeWalker(repo)
    found = walker.walk("src/v.py", "__title__ = 'x'")
    assert {s.subject[:1]: s.kind for s in found} == {"A": "origin", "B": "deletion", "R": "origin"}
    assert "v.py" in walker.paths  # the pre-rename path was discovered


def test_pickaxe_search_unions_follow_and_full_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    On psf/requests, `git log -G --follow` skipped two commits that a
    `--full-history` query over the file's old path found. The search must run
    both and union the results (newest first).
    """

    def header(sha: str, ts: int, subject: str) -> str:
        return history.SEP.join(
            [
                history.HEADER_PREFIX,
                sha * 40,
                sha * 7,
                "a",
                "a@x",
                "2017-05-25T00:00:00+00:00",
                str(ts),
                "p",
                subject,
                "",
            ]
        )

    def entry(sha: str, ts: int, subject: str, path: str, sign: str) -> str:
        return "\n".join(
            [
                header(sha, ts, subject),
                f"diff --git a/{path} b/{path}",
                f"--- a/{path}",
                f"+++ b/{path}",
                "@@ -1 +1 @@",
                f"{sign}__title__ = 'x'",
            ]
        )

    calls: list[list[str]] = []

    def fake_run_git(args: list[str], cwd: Path, **_: object) -> str:
        calls.append(args)
        if "--follow" in args:
            assert args[-1] == "src/v.py"
            return entry("a", 1, "A: created", "v.py", "+")
        assert "--full-history" in args
        assert args[args.index("--") + 1 :] == ["src/v.py", "v.py"]  # discovered path included
        return "\n".join(
            [
                entry("c", 3, "C: re-created", "v.py", "+"),
                entry("b", 2, "B: deleted", "v.py", "-"),
                entry("a", 1, "A: created", "v.py", "+"),
            ]
        )

    monkeypatch.setattr(history, "run_git", fake_run_git)
    steps = history.pickaxe_search(tmp_path, "src/v.py", "^__title__ = 'x'$")
    assert [s.subject[:1] for s in steps] == ["C", "B", "A"]
    assert len(calls) == 2
