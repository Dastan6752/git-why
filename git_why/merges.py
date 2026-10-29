"""
merges.py — merge commits and what they mean for a line's history.

Three jobs:

1. **Landing merges** (always on, cheap). For each commit on a side branch, find
   the merge that brought it onto the current branch's first-parent history.
   Merge subjects like ``Merge pull request #2216 from ...`` are often the only
   place the PR number is recorded, so this feeds the GitHub integration too.

2. **Filling in merge hunks** (always on). ``git log -L`` lists a merge commit
   when the merge result differs from *every* parent (a conflict resolution),
   but prints no diff for it. We fetch the merge's own edits so the timeline
   can show what the resolution actually did.

3. **Conflict-resolution scan** (``--merges``). ``git log -L`` and the pickaxe
   never look at merges that simply picked one side — yet "picking one side"
   can silently drop a line the other side added. With ``--merges`` we diff
   every merge the file was involved in using ``git --remerge-diff`` (git ≥
   2.36: automatic merge result vs. what was committed) and report edits to
   our line that no ordinary commit explains. On older git the fallback is a
   first-parent diff, de-duplicated against the side branch's own commits.

Ordering: timelines are sorted newest-first by commit time; commits made in
the same second are ordered topologically with ``git merge-base``.
"""

from __future__ import annotations

from functools import cmp_to_key
from pathlib import Path

from .history import Step, commit_details, is_ancestor, mainline, merge_commit_diff, merges_between, pickaxe_search
from .pickaxe import MAX_DEPTH, anchored_regex, worth_searching

MAX_MERGE_SEARCHES = MAX_DEPTH + 1
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>", "|||||||")


def annotate_landing_merges(repo: Path, steps: list[Step]) -> None:
    """Fill in ``step.landed_via`` for every step that is not on the mainline."""
    if not steps:
        return
    try:
        main = set(mainline(repo))
    except Exception:
        return
    cache: dict[str, Step | None] = {}
    for step in steps:
        if step.sha in main:
            continue
        for merge_sha in merges_between(repo, step.sha):
            if merge_sha in main:
                if merge_sha not in cache:
                    cache[merge_sha] = commit_details(repo, merge_sha)
                step.landed_via = cache[merge_sha]
                break


def historical_paths(file: str, steps: list[Step]) -> list[str]:
    """Every path the file has had, current one first (for commands without --follow)."""
    paths = [file]
    for s in steps:
        for p in (s.file_before, s.file_after):
            if p and p not in paths:
                paths.append(p)
    return paths


def fill_merge_hunks(repo: Path, file: str, steps: list[Step], lineage: set[str]) -> None:
    """Give hunk-less merge steps (as produced by ``git log -L``) their real edits."""
    paths = historical_paths(file, steps)
    for step in steps:
        if not step.is_merge or step.hunks:
            continue
        diff = merge_commit_diff(repo, step.sha, paths)
        if diff is None:
            continue
        removed, added = _relevant(diff, lineage)
        if removed or added:
            step.focus(removed, added)
            step.file_before, step.file_after = diff.file_before, diff.file_after


def scan_merge_resolutions(repo: Path, file: str, lineage: set[str], existing: list[Step]) -> list[Step]:
    """
    Merge commits that edited one of the ``lineage`` texts in ``file`` in a way
    not already explained by a commit in ``existing``.
    """
    known = {_signature(s) for s in existing}
    known_shas = {s.sha for s in existing}
    paths = historical_paths(file, existing)
    found: dict[str, Step] = {}

    texts = [t for t in sorted(lineage) if worth_searching(t)][:MAX_MERGE_SEARCHES]
    for text in texts:
        for step in pickaxe_search(repo, file, anchored_regex(text), merges=True, extra_paths=paths):
            if step.sha in known_shas or step.sha in found:
                continue
            removed, added = _relevant(step, lineage)
            if not removed and not added:
                continue
            step.focus(removed, added)
            if _signature(step) in known:
                continue  # first-parent fallback: the side branch's change landing — nothing new
            step.found_by = "merge"
            found[step.sha] = step
    return list(found.values())


def order_newest_first(repo: Path, steps: list[Step]) -> list[Step]:
    """Sort by commit time, resolving same-second ties with ancestry."""
    steps = sorted(steps, key=lambda s: s.timestamp, reverse=True)
    out: list[Step] = []
    i = 0
    while i < len(steps):
        j = i
        while j + 1 < len(steps) and steps[j + 1].timestamp == steps[i].timestamp:
            j += 1
        group = steps[i : j + 1]
        if len(group) > 1:

            def cmp(a: Step, b: Step) -> int:
                if a.sha == b.sha:
                    return 0
                if is_ancestor(repo, a.sha, b.sha):
                    return 1  # a is older -> after b
                if is_ancestor(repo, b.sha, a.sha):
                    return -1
                return 0

            group = sorted(group, key=cmp_to_key(cmp))
        out.extend(group)
        i = j + 1
    return out


# ---------------------------------------------------------------------------


def _relevant(step: Step, lineage: set[str]) -> tuple[list[str], list[str]]:
    """Lines of the merge's edit that concern our line (conflict markers dropped)."""

    def keep(line: str) -> bool:
        t = line.strip()
        if t.startswith(CONFLICT_MARKERS):
            return False
        return not lineage or t in lineage

    removed = [line for line in step.removed if keep(line)]
    added = [line for line in step.added if keep(line)]
    if lineage and not removed and not added:
        # Our texts may not all be known (e.g. the walk could not pass this
        # merge). Fall back to everything that is not a conflict marker.
        removed = [line for line in step.removed if not line.strip().startswith(CONFLICT_MARKERS)]
        added = [line for line in step.added if not line.strip().startswith(CONFLICT_MARKERS)]
    return removed, added


def _signature(step: Step) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(sorted(line.strip() for line in step.removed)),
        tuple(sorted(line.strip() for line in step.added)),
    )
