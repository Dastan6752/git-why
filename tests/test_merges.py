"""Landing-merge annotation and the --merges conflict-resolution scan."""

from __future__ import annotations

from pathlib import Path

from git_why import history
from git_why.core import Options, investigate
from git_why.merges import annotate_landing_merges, scan_merge_resolutions
from git_why.pickaxe import PickaxeWalker, merge_timelines


def test_landing_merge_is_found_for_side_branch_commits(blind_spot_repo: Path) -> None:
    walker = PickaxeWalker(blind_spot_repo)
    walker.walk("cfg.py", "RETRIES = 0")
    steps = merge_timelines(history.line_history(blind_spot_repo, "cfg.py", 2, 2), walker.found, walker.lineage)
    annotate_landing_merges(blind_spot_repo, steps)

    by_subject = {s.subject[:1]: s for s in steps}
    assert by_subject["A"].landed_via is None  # on mainline
    assert by_subject["B"].landed_via is not None
    assert by_subject["B"].landed_via.subject.startswith("Merge pull request #7")
    assert by_subject["C"].landed_via is by_subject["B"].landed_via  # cached object


def test_landing_merge_exposes_pr_reference(blind_spot_repo: Path) -> None:
    report = investigate(blind_spot_repo / "cfg.py", 2, 2, Options())
    b = next(s for s in report.steps if s.subject.startswith("B"))
    refs = report.refs(b)
    assert any(r.kind == "pr" and r.id == "7" and r.verb == "landed via" for r in refs)
    assert any(r.kind == "issue" and r.id == "42" and r.verb == "fixes" for r in refs)


def test_evil_merge_is_visible_even_without_merges_flag(evil_merge_repo: Path) -> None:
    """git log -L shows merges whose result differs from every parent."""
    report = investigate(evil_merge_repo / "cfg.py", 2, 2, Options())
    merge = next(s for s in report.steps if s.subject.startswith("M:"))
    assert merge.is_merge
    assert merge.added == ["TIMEOUT = 30"]
    assert set(merge.removed) == {"TIMEOUT = 15", "TIMEOUT = 20"}
    # the walk followed both sides of the conflict back to their origins
    assert [s.subject[:1] for s in reversed(report.steps)] == ["A", "B", "C", "M"]
    assert report.lineage >= {"TIMEOUT = 10", "TIMEOUT = 15", "TIMEOUT = 20", "TIMEOUT = 30"}


def test_merge_that_drops_a_line_needs_merges_flag(merge_drop_repo: Path) -> None:
    without = investigate(merge_drop_repo / "cfg.py", 3, 3, Options())
    assert not any(s.found_by == "merge" for s in without.steps)

    with_flag = investigate(merge_drop_repo / "cfg.py", 3, 3, Options(merges=True))
    merge = next(s for s in with_flag.steps if s.found_by == "merge")
    assert merge.subject.startswith("M: merge feature")
    assert merge.is_deletion and merge.removed == ["FEATURE_FLAG = True"]
    # ordering oldest-first: B adds, M drops, R re-adds
    subjects = [s.subject[:1] for s in reversed(with_flag.steps) if not s.is_noise]
    assert subjects == ["B", "M", "R"]
    assert with_flag.merges_scanned
    assert "1 change made inside merge commits" in with_flag.summary()


def test_merge_scan_skips_plain_branch_landings(blind_spot_repo: Path) -> None:
    """A merge whose first-parent diff just equals the side branch's change is not reported."""
    steps = history.line_history(blind_spot_repo, "cfg.py", 2, 2)
    walker = PickaxeWalker(blind_spot_repo)
    walker.walk("cfg.py", "RETRIES = 0")
    steps = merge_timelines(steps, walker.found, walker.lineage)
    extra = scan_merge_resolutions(blind_spot_repo, "cfg.py", walker.lineage, steps)
    assert extra == []


def test_merge_scan_uses_historical_paths(make_repo) -> None:  # type: ignore[no-untyped-def]
    """The file was renamed after a merge dropped the line: the scan must look at the old path."""
    from tests.conftest import git, write_commit

    repo = make_repo("renamed")
    write_commit(repo, "old.py", "import os\n", "A")
    git(repo, "checkout", "-q", "-b", "feature")
    write_commit(repo, "old.py", "import os\nFLAG = True\n", "B: add flag")
    git(repo, "checkout", "-q", "main")
    write_commit(repo, "old.py", "import sys\nimport os\n", "C")
    git(repo, "merge", "--no-commit", "--no-ff", "feature", check=False)
    (repo / "old.py").write_text("import sys\nimport os\n")
    git(repo, "add", "old.py")
    git(repo, "commit", "-q", "-m", "M: merge (flag lost)")
    git(repo, "mv", "old.py", "new.py")
    git(repo, "commit", "-q", "-m", "rename")
    write_commit(repo, "new.py", "import sys\nimport os\nFLAG = True\n", "R: re-add flag")

    report = investigate(repo / "new.py", 3, 3, Options(merges=True))
    assert any(s.found_by == "merge" and s.is_deletion for s in report.steps)
