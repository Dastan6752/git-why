"""
refs.py — find references to issues, PRs and tickets inside commit messages.

Commit messages are where developers leave their reasoning, but it is usually
one hop away: "Fixes #412". This module pulls those pointers out so the
timeline can show them and ``--github`` can fetch the discussion behind them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .history import Step


@dataclass(frozen=True)
class Ref:
    kind: str  # "issue", "pr", "ticket"
    id: str  # "412", "PAY-77"
    verb: str  # "fixes", "closes", "see", "" (how it was mentioned)
    repo: str | None = None  # "owner/name" when the reference was cross-repo

    @property
    def number(self) -> int | None:
        return int(self.id) if self.id.isdigit() else None

    def __str__(self) -> str:
        label = {"issue": "#", "pr": "PR #", "ticket": ""}[self.kind] + self.id
        if self.repo:
            label = f"{self.repo}{label}" if self.kind != "ticket" else label
        return f"{self.verb} {label}".strip()


# "Fixes #412", "closes: #9", "resolve #12", "fixes owner/repo#12"
_CLOSING = re.compile(
    r"\b(fix(?:es|ed)?|close[sd]?|resolve[sd]?)\b:?\s+(?:([\w.-]+/[\w.-]+))?#(\d+)",
    re.IGNORECASE,
)
# "PR #1180", "pull request #7", "(#1180)" at the end of squash-merge subjects
_PR = re.compile(r"\b(?:PR|pull request)\s*#(\d+)|\(#(\d+)\)\s*$", re.IGNORECASE | re.MULTILINE)
# "Merge pull request #2216 from ..."
_MERGE_PR = re.compile(r"^Merge pull request #(\d+)", re.IGNORECASE | re.MULTILINE)
# GitHub URLs
_URL = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)/(issues|pull)/(\d+)")
# plain "#123" or "GH-123" anywhere
_PLAIN = re.compile(r"(?<![\w&/])(?:#|GH-)(\d+)\b", re.IGNORECASE)
# Jira-style tickets: PAY-77, ABC-1234 (but not GH-123, handled above)
_TICKET = re.compile(r"\b(?!GH-)([A-Z][A-Z0-9]{1,9}-\d+)\b")


def extract(message: str) -> list[Ref]:
    """Return references found in a commit message, most meaningful first."""
    found: list[Ref] = []
    seen: set[tuple[str, str | None]] = set()

    def add(ref: Ref) -> None:
        key = (ref.id, ref.repo)
        if key not in seen:
            seen.add(key)
            found.append(ref)

    for num in _MERGE_PR.findall(message):
        add(Ref("pr", num, "merged"))
    for verb, repo, num in _CLOSING.findall(message):
        add(Ref("issue", num, verb.lower(), repo or None))
    for repo, kind, num in _URL.findall(message):
        add(Ref("pr" if kind == "pull" else "issue", num, "", repo))
    for a, b in _PR.findall(message):
        add(Ref("pr", a or b, ""))
    for num in _PLAIN.findall(message):
        add(Ref("issue", num, ""))
    for ticket in _TICKET.findall(message):
        add(Ref("ticket", ticket, ""))

    return found


def refs_for(step: Step) -> list[Ref]:
    refs = extract(step.message)
    if step.landed_via is not None:
        for ref in extract(step.landed_via.message):
            if ref.kind == "pr" and all(r.id != ref.id for r in refs):
                refs.append(Ref(ref.kind, ref.id, "landed via", ref.repo))
    return refs
