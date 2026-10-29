"""Report model: confidence, summary, evidence strength, serialisation."""

from __future__ import annotations

import json
from pathlib import Path

from git_why.github import Comment, Thread
from git_why.history import Hunk, RepoInfo, Step
from git_why.report import Report, Target


def step(sha: str, ts: int, subject: str = "s", body: str = "", **kw) -> Step:  # type: ignore[no-untyped-def]
    s = Step(sha * 40, sha * 7, "ann", "a@x", "2024-01-01T00:00:00+00:00", ts, subject, body)
    s.hunks = [Hunk(removed=kw.get("removed", []), added=kw.get("added", ["x = 1"]))]
    s.found_by = kw.get("found_by", "range")
    s.is_noise = kw.get("noise", False)
    s.noise_reason = "fmt" if s.is_noise else ""
    return s


def repo(shallow: bool = False, boundaries: set[str] | None = None) -> RepoInfo:
    return RepoInfo(Path("/r"), "h" * 40, shallow, boundaries or set(), None)


def report(steps: list[Step], **kw) -> Report:  # type: ignore[no-untyped-def]
    defaults = dict(
        target=Target("f.py", 1, 1, ["x = 1"]), repo=repo(), steps=steps, pickaxe_used=True, lineage_complete=True
    )
    defaults.update(kw)
    return Report(**defaults)  # type: ignore[arg-type]


def test_confidence_high_when_complete() -> None:
    r = report([step("b", 2, removed=["x = 0"]), step("a", 1)])
    c = r.confidence()
    assert c.level == "high"
    assert any("--merges" in x for x in c.reasons)


def test_confidence_medium_when_lineage_incomplete() -> None:
    r = report([step("b", 2, removed=["x = 0"]), step("a", 1)], lineage_complete=False)
    assert r.confidence().level == "medium"


def test_confidence_medium_when_oldest_is_a_change() -> None:
    r = report([step("a", 1, removed=["x = 0"])])
    c = r.confidence()
    assert c.level == "medium" and any("earlier version" in x for x in c.reasons)


def test_confidence_low_for_shallow_boundary() -> None:
    s = step("a", 1)
    s.shallow_boundary = True
    r = report([s], repo=repo(shallow=True, boundaries={s.sha}))
    assert r.confidence().level == "low"


def test_confidence_low_without_steps() -> None:
    assert report([]).confidence().level == "low"


def test_summary_counts() -> None:
    steps = [
        step("d", 4, "fmt", noise=True),
        step("c", 3, "merge fix", removed=["x = 2"], added=["x = 3"], found_by="merge"),
        step("b", 2, "change", removed=["x = 0"], found_by="pickaxe", body="Fixes #9"),
        step("a", 1, "born"),
    ]
    text = report(steps).summary()
    assert text.startswith('Introduced on 2024-01-01 by ann ("born")')
    assert "changed meaningfully 2 times" in text
    assert "1 formatting/move commit ignored" in text
    assert "1 change made inside merge commits" in text
    assert "1 commit recovered" in text


def test_summary_in_text_mode_does_not_claim_blame_comparison() -> None:
    steps = [step("a", 1, found_by="pickaxe")]
    r = report(steps, target=Target("f.py", 0, 0, ["x = 1"], text="x = 1"))
    assert "recovered" not in r.summary()
    assert r.hidden_count == 0


def test_evidence_strength_levels() -> None:
    weak = step("a", 1, "tweak")
    medium = step("b", 2, "tweak", body="Because the previous value caused timeouts under load.")
    strong = step("c", 3, "tweak (#12)", body="Because the previous value caused timeouts under load.")
    r = report([strong, medium, weak])
    assert r.evidence_strength(weak) == "weak"
    assert r.evidence_strength(medium) == "medium"
    assert r.evidence_strength(strong) == "strong"

    # a fetched thread with discussion upgrades to strong even with a bare subject
    refd = step("d", 4, "see #5")
    r = report([refd])
    r.threads[5] = Thread(5, "issue", "t", "closed", "u", "me", comments=[Comment("x", "why", "u", "comment")])
    assert r.evidence_strength(refd) == "strong"


def test_to_dict_is_json_serialisable_and_oldest_first() -> None:
    r = report([step("b", 2, removed=["x = 0"]), step("a", 1)])
    data = json.loads(json.dumps(r.to_dict()))
    assert [s["short_sha"][0] for s in data["steps"]] == ["a", "b"]
    assert data["steps"][0]["url"] is None  # no remote
    assert set(data) >= {
        "schema_version",
        "target",
        "repo",
        "strategies",
        "confidence",
        "summary",
        "steps",
        "github",
        "warnings",
    }


def test_target_labels() -> None:
    assert Target("f.py", 4, 4, []).label == "f.py:4"
    assert Target("f.py", 4, 9, []).label == "f.py:4-9"
    assert Target("f.py", 4, 9, [], function="go").label == "f.py --function go (lines 4-9)"
    assert Target("f.py", 0, 0, [], text="a = 1").label == 'f.py (text "a = 1")'
