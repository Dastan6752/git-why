"""
noise.py — decide which commits are "noise" and which are real decisions.

A commit is noise if it touched the line but didn't *mean* anything:
  * formatter / whitespace-only changes
  * a file rename or move where the text itself is identical
  * (later) mass renames of a variable, import reordering, etc.

We keep noise commits in the output but dim them, because sometimes "when did
this file get moved?" is itself the question.
"""

from __future__ import annotations

import re

from .history import Step

# Words in a commit subject that strongly suggest a no-op change.
NOISE_WORDS = re.compile(
    r"\b(fmt|format|formatting|black|prettier|lint|linting|style|whitespace|"
    r"reformat|autopep8|isort|rename|move|moved|relocate)\b",
    re.IGNORECASE,
)


def _squash(lines: list[str]) -> str:
    """Remove ALL whitespace so 'a  *  b' and 'a * b' compare equal."""
    return "".join("".join(line.split()) for line in lines)


def classify(step: Step) -> Step:
    """Fill in step.is_noise / step.noise_reason and return the step."""

    # 0. pickaxe.py may already have decided (e.g. "moved within file").
    if step.is_noise:
        return step

    # 1. Line was created here — that's never noise, it's the origin story.
    if step.is_origin:
        step.is_noise = False
        return step

    before = _squash(step.removed)
    after = _squash(step.added)

    # 2. Same text once whitespace is ignored => formatting-only.
    if before == after and (step.removed or step.added):
        step.is_noise = True
        step.noise_reason = "whitespace / formatting only"
        return step

    # 3. File path changed but content didn't => pure move/rename.
    if step.file_before != step.file_after and before == after:
        step.is_noise = True
        step.noise_reason = f"moved from {step.file_before}"
        return step

    # 4. Content changed, but the message screams "style" and the change is tiny
    #    (e.g. quote style, trailing comma). Be conservative: only if almost equal.
    if NOISE_WORDS.search(step.subject) and _similar(before, after):
        step.is_noise = True
        step.noise_reason = "looks like a style-only change"
        return step

    step.is_noise = False
    return step


def _similar(a: str, b: str, threshold: float = 0.9) -> bool:
    """Cheap similarity check: share of characters in common."""
    if not a or not b:
        return False
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio() >= threshold


def classify_all(steps: list[Step]) -> list[Step]:
    return [classify(s) for s in steps]
