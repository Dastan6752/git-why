"""
Tests that don't need a real git repo: parsing, noise detection, refs.
Run with:  pytest
"""

from git_why import noise, refs
from git_why.cli import parse_target
from git_why.history import HEADER_PREFIX, SEP, Hunk, Step, parse_log


def make_step(removed=(), added=(), **kw) -> Step:
    base = dict(sha="a" * 40, short_sha="aaaaaaa", author="x", email="x@y",
                date="2024-01-01T00:00:00+00:00", timestamp=0, subject="msg", body="",
                hunks=[Hunk(removed=list(removed), added=list(added))])
    base.update(kw)
    return Step(**base)


# ---------- parse_target ----------

def test_parse_single_line():
    assert parse_target("src/app.py:42") == ("src/app.py", 42, 42)

def test_parse_range_and_swapped():
    assert parse_target("app.py:9-3") == ("app.py", 3, 9)

def test_parse_windows_like_path_with_colon_in_dir():
    assert parse_target("a:b/c.py:7") == ("a:b/c.py", 7, 7)


# ---------- noise ----------

def test_origin_is_never_noise():
    s = make_step(file_before=None, file_after="a.py", added=["x = 1"])
    assert noise.classify(s).is_noise is False

def test_whitespace_only_is_noise():
    s = make_step(file_before="a.py", file_after="a.py",
                  removed=["x=1"], added=["x = 1"])
    assert noise.classify(s).is_noise is True
    assert "formatting" in s.noise_reason

def test_real_change_is_not_noise():
    s = make_step(file_before="a.py", file_after="a.py",
                  removed=["sleep(1.0)"], added=["sleep(0.3)"])
    assert noise.classify(s).is_noise is False


# ---------- refs ----------

def test_refs_fixes_issue():
    r = refs.extract("add retry\n\nFixes #412: double charges")
    assert [(x.kind, x.id, x.verb) for x in r] == [("issue", "412", "fixes")]

def test_refs_pr_and_ticket():
    r = refs.extract("reduce backoff (#1180)\n\nsee PAY-77")
    kinds = {(x.kind, x.id) for x in r}
    assert ("pr", "1180") in kinds
    assert ("ticket", "PAY-77") in kinds

def test_refs_no_duplicates():
    r = refs.extract("Fixes #5, also #5 again")
    assert len(r) == 1


# ---------- git log parser ----------

def test_parse_git_output():
    header = SEP.join([HEADER_PREFIX, "f" * 40, "fffffff", "dima", "d@x",
                       "2024-01-01T00:00:00+00:00", "1704067200", "add thing", "Fixes #1"])
    text = "\n".join([
        header,
        "second body line",
        "- a bullet that must NOT be parsed as a removed line",
        "",
        "diff --git a/old.py b/new.py",
        "--- a/old.py",
        "+++ b/new.py",
        "@@ -8,1 +8,1 @@",
        "-old",
        "+new",
    ])
    (step,) = parse_log(text)
    assert step.short_sha == "fffffff"
    assert step.timestamp == 1704067200
    assert step.file_before == "old.py" and step.file_after == "new.py"
    assert step.removed == ["old"] and step.added == ["new"]
    assert "second body line" in step.body
    assert "a bullet" in step.body


def test_parse_multiple_hunks():
    header = SEP.join([HEADER_PREFIX, "f" * 40, "fffffff", "a", "a@x",
                       "2024-01-01T00:00:00+00:00", "1", "s", ""])
    text = "\n".join([header, "diff --git a/f b/f", "--- a/f", "+++ b/f",
                      "@@ -1 +1 @@", "-one", "+uno",
                      "@@ -9 +9 @@", "-nine", "+nueve"])
    (step,) = parse_log(text)
    assert len(step.hunks) == 2
    assert step.hunks[1].removed == ["nine"]
    assert step.removed == ["one", "nine"]  # flattened view
