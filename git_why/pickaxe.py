"""
pickaxe.py — the "pickaxe walk": find commits that `git log -L` missed.

Why is this needed?
-------------------
`git log -L` applies *history simplification*: at a merge commit, if one parent
already has the exact same text, git follows only that parent. So a change that
happened on a side branch and was later changed back (or converged with
mainline) silently disappears. `git blame` has the same blind spot.

`git log -G<regex>` (the pickaxe) has no such simplification — it looks at the
diff of EVERY commit. Its limitation: it only knows about one text. So we walk:

    walk(text):
        for each commit that ADDED `text`:
            record it
            look in the same hunk for the REMOVED line most similar to `text`
            if found:  walk(that older text)       <- recursion
            else:      this is where the line was born

Each hop goes one version further into the past. A cache of texts already
searched keeps round-trips (A -> B -> A) from looping forever.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

from .history import GitError, Step, pickaxe_search, run_git

MAX_DEPTH = 8  # how many text versions back we are willing to go
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>", "|||||||")
MIN_SIMILARITY = 0.45  # how alike a removed line must be to count as "the previous version"
MIN_SIGNAL_CHARS = 6  # don't pickaxe-search super generic lines like "}" or "pass"

# Lines so common that searching for them would return half the repo.
BORING = {
    "",
    "}",
    "{",
    "};",
    ")",
    ");",
    "]",
    "pass",
    "else:",
    "else",
    "return",
    "return None",
    "break",
    "continue",
    "end",
    "fi",
    "done",
    "/>",
    "</div>",
    '"""',
    "'''",
    "*/",
    "/*",
    "#",
    "//",
}


def worth_searching(text: str) -> bool:
    """Is this line distinctive enough for a pickaxe search to be useful?"""
    t = text.strip()
    if t in BORING:
        return False
    signal = sum(ch.isalnum() for ch in t)
    return signal >= MIN_SIGNAL_CHARS


def ere_escape(text: str) -> str:
    """Escape for POSIX extended regex (what git -G uses). Python's re.escape
    adds backslashes git's regex engine may not understand, so roll our own."""
    return re.sub(r"([\\.\[\]{}()*+?^$|])", r"\\\1", text)


def anchored_regex(text: str) -> str:
    """Match the whole line, ignoring leading/trailing whitespace differences."""
    return "^[[:space:]]*" + ere_escape(text.strip()) + "[[:space:]]*$"


def same(a: str, b: str) -> bool:
    return a.strip() == b.strip()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.strip(), b.strip()).ratio()


def _count(content: str, text: str) -> int:
    t = text.strip()
    return sum(1 for line in content.splitlines() if line.strip() == t)


class PickaxeWalker:
    def __init__(self, repo: Path):
        self.repo = repo
        self._cache: dict[tuple[str, str], list[Step]] = {}  # (file, text) -> results
        self.seen: set[str] = set()  # commit shas already recorded
        self.found: list[Step] = []
        # Every text the line has ever had (stripped). This is the "lineage" and
        # lets us tell which lines in a big -L hunk are really about our line.
        self.lineage: set[str] = set()
        # Historical paths of the file (learned from diffs); merge searches
        # cannot use --follow, so they need these explicitly.
        self.paths: set[str] = set()
        self.searches = 0  # for --debug / curiosity

    # -- public ---------------------------------------------------------------

    def walk(self, file: str, text: str, current_content: str | None = None) -> list[Step]:
        """
        Trace `text` (current content of a line in `file`) back to its birth.
        `current_content` is the file as it is now; if `text` occurs more than
        once in it we cannot know which occurrence is ours, so we don't walk.
        """
        if current_content is not None and _count(current_content, text) > 1:
            self.lineage.add(text.strip())
            return self.found
        self._walk(file, text, depth=0)
        return self.found

    # -- internals ------------------------------------------------------------

    def _unambiguous_before(self, step: Step, text: str) -> bool:
        """Did `text` occur exactly once in the file just before `step`?
        If it occurred several times, the pickaxe can't tell them apart."""
        path = step.file_before
        if path is None:
            return True
        try:
            content = run_git(["show", f"{step.sha}^:{path}"], cwd=self.repo)
        except GitError:
            return True  # e.g. root commit; be permissive
        return _count(content, text) <= 1

    def _search(self, file: str, text: str, *, merges: bool = False) -> list[Step]:
        key = (("merges:" if merges else "") + file, text.strip())
        if key not in self._cache:
            self.searches += 1
            self._cache[key] = pickaxe_search(
                self.repo, file, anchored_regex(text), merges=merges, extra_paths=sorted(self.paths)
            )
        return self._cache[key]

    def _walk(self, file: str, text: str, depth: int) -> None:
        self.lineage.add(text.strip())
        if depth > MAX_DEPTH or not worth_searching(text):
            return

        introduced = self._walk_steps(self._search(file, text), file, text, depth)
        if not introduced:
            # No ordinary commit added this text. A merge may have (conflict
            # resolution / "evil merge"); those are invisible to plain -G.
            self._walk_steps(self._search(file, text, merges=True), file, text, depth)

    def _walk_steps(self, candidates: list[Step], file: str, text: str, depth: int) -> bool:
        """Process search results for ``text``. Returns True if some commit added it."""
        introduced = False
        for step in candidates:
            for p in (step.file_before, step.file_after):
                if p:
                    self.paths.add(p)
            if step.sha in self.seen:
                continue

            # Find the hunk where this text was ADDED.
            hit = next((h for h in step.hunks if any(same(l, text) for l in h.added)), None)
            if hit is None:
                # The commit only REMOVED the text. Either it was changed into a
                # newer version (that story is told when walking from the newer
                # text, so skip) or it was simply deleted — worth recording,
                # because the line evidently came back later.
                gone = next((h for h in step.hunks if any(same(l, text) for l in h.removed)), None)
                if gone is not None and self._best_predecessor(gone.added, text) is None:
                    self.seen.add(step.sha)
                    step.found_by = "merge" if step.is_merge else "pickaxe"
                    step.focus([next(l for l in gone.removed if same(l, text))], [])
                    self.found.append(step)
                continue

            introduced = True
            self.seen.add(step.sha)
            step.found_by = "merge" if step.is_merge else "pickaxe"
            added_line = next(l for l in hit.added if same(l, text))

            # Was the exact same text also removed in this hunk? Then the
            # commit just moved the line around — note it, but keep searching
            # older commits for the same text instead of recursing.
            if any(same(l, text) for l in hit.removed):
                step.focus([added_line], [added_line])
                step.is_noise, step.noise_reason = True, "moved within file"
                self.found.append(step)
                continue

            # Otherwise: which removed line is the previous version of ours?
            removed_lines = [l for l in hit.removed if not l.strip().startswith(CONFLICT_MARKERS)]
            predecessor = self._best_predecessor(removed_lines, text)
            if step.is_merge:
                # A conflict resolution replaced *both* sides' versions; show them all.
                step.focus(removed_lines, [added_line])
            else:
                step.focus([predecessor] if predecessor is not None else [], [added_line])
            self.found.append(step)

            if predecessor is None:
                continue  # born here — nothing older to look for along this path
            if step.is_merge:
                for older in removed_lines:  # follow every side of the conflict
                    self._walk(file, older, depth + 1)
                continue
            if not self._unambiguous_before(step, predecessor):
                # Several identical lines existed back then — stop here and
                # let git log -L carry the older history.
                self.lineage.add(predecessor.strip())
                continue
            # Keep searching from the *current* path: --follow already walks
            # renames backwards from HEAD, and this keeps the cache warm.
            self._walk(file, predecessor, depth + 1)
        return introduced

    @staticmethod
    def _best_predecessor(candidates: list[str], text: str) -> str | None:
        best, best_score = None, 0.0
        for c in candidates:
            s = similarity(c, text)
            if s > best_score:
                best, best_score = c, s
        return best if best_score >= MIN_SIMILARITY else None


# ---------------------------------------------------------------------------
# Merging the two strategies
# ---------------------------------------------------------------------------


def merge_timelines(
    range_steps: list[Step],
    pickaxe_steps: list[Step],
    lineage: set[str] | None = None,
) -> list[Step]:
    """
    Union by commit sha, newest first.

    `lineage` (from PickaxeWalker) is used to fix git log -L's *range drift*:
    when a commit replaces N lines with M lines, -L widens the range it tracks,
    and older commits then show changes to neighbouring lines that have nothing
    to do with ours. With the lineage we can keep only the relevant lines and
    drop steps that touched none of them.
    """
    if lineage:
        range_steps = _trim_to_lineage(range_steps, pickaxe_steps, lineage)

    by_sha: dict[str, Step] = {s.sha: s for s in pickaxe_steps}
    by_sha.update({s.sha: s for s in range_steps})  # -L wins on shared commits
    return sorted(by_sha.values(), key=lambda s: s.timestamp, reverse=True)


def _trim_to_lineage(range_steps: list[Step], pickaxe_steps: list[Step], lineage: set[str]) -> list[Step]:
    if not pickaxe_steps:
        return range_steps  # nothing to cross-check against

    # If the walk never reached the line's birth (too generic / too deep /
    # ambiguous), the lineage is incomplete. Then only trim steps newer than the
    # oldest commit the walk reached; older -L steps are kept rather than risk
    # losing real history.
    complete = any(s.is_origin for s in pickaxe_steps)
    horizon = min(s.timestamp for s in pickaxe_steps)

    kept: list[Step] = []
    for step in range_steps:
        if not complete and step.timestamp <= horizon:
            kept.append(step)
            continue
        removed = [l for l in step.removed if l.strip() in lineage]
        added = [l for l in step.added if l.strip() in lineage]
        if removed or added:
            step.focus(removed, added)
            kept.append(step)
        # else: range drift — this commit never touched our line.
    return kept
