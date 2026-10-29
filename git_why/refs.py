"""
refs.py — find references to issues, PRs and tickets inside commit messages.

Commit messages are where developers leave their reasoning, but it is usually
one hop away: "Fixes #412". This module pulls those pointers out so the
timeline can show them (and so a later version can fetch the actual issue
text from GitHub / GitLab / Jira).
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

    def __str__(self) -> str:
        label = {"issue": "#", "pr": "PR #", "ticket": ""}[self.kind] + self.id
        return f"{self.verb} {label}".strip()


# "Fixes #412", "closes: #9", "resolve #12"
_CLOSING = re.compile(
    r"\b(fix(?:es|ed)?|close[sd]?|resolve[sd]?)\b:?\s+#(\d+)", re.IGNORECASE
)
# "PR #1180", "pull request #7", "(#1180)" at end of squash-merge subjects
_PR = re.compile(r"\b(?:PR|pull request)\s*#(\d+)|\(#(\d+)\)\s*$", re.IGNORECASE | re.MULTILINE)
# plain "#123" anywhere
_PLAIN = re.compile(r"(?<![\w&])#(\d+)\b")
# Jira-style tickets: PAY-77, ABC-1234
_TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")


def extract(message: str) -> list[Ref]:
    """Return references found in a commit message, most meaningful first."""
    found: list[Ref] = []
    seen: set[str] = set()

    def add(ref: Ref) -> None:
        if ref.id not in seen:
            seen.add(ref.id)
            found.append(ref)

    for verb, num in _CLOSING.findall(message):
        add(Ref("issue", num, verb.lower()))
    for a, b in _PR.findall(message):
        add(Ref("pr", a or b, ""))
    for num in _PLAIN.findall(message):
        add(Ref("issue", num, ""))
    for ticket in _TICKET.findall(message):
        add(Ref("ticket", ticket, ""))

    return found


def refs_for(step: Step) -> list[Ref]:
    return extract(step.message)
