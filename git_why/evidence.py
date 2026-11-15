"""
Turn a :class:`~git_why.report.Report` into a numbered **evidence pack** for ``--ask``.

Every citable item gets a stable ID the model must quote:

* ``C1, C2, …`` commits (oldest first, noise excluded),
* ``P1, …`` pull requests and ``I1, …`` issues fetched with ``--github``,
* ``R1, …`` review / issue comments on those threads,
* ``S1, S2`` the source code at HEAD: the target line(s) and the enclosing block.

The pack is the *only* thing the model sees. It is built from fields that are
already in the JSON report, so an answer can always be traced back to the
normal ``git why`` output. Pure functions; no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .github import Thread
from .history import Step
from .report import Report

DEFAULT_BUDGET = 6_000  # characters of evidence text handed to the model
MAX_CODE_CHARS = 1_500  # the enclosing block shown for code questions
MODE_HISTORY = "history"  # why / when / who: commits, PRs and comments first, then the line itself
MODE_CODE = "code"  # what / how: the source at HEAD first, then the commits
MAX_BODY_CHARS = 400  # per commit message / thread description
MAX_COMMENT_CHARS = 300


@dataclass
class Item:
    id: str  # "C1", "P1", "I1", "R1"
    kind: str  # "commit" | "pr" | "issue" | "comment" | "code"
    label: str  # short human label for the Sources list
    text: str  # what the model reads
    url: str | None = None
    quote: str = ""  # the exact human-written text (comment body, commit subject) - for verbatim display

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "url": self.url}


@dataclass
class EvidencePack:
    target: str  # "src/app.py:42"
    line: str  # current text of the line
    items: list[Item] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)  # history-completeness notes
    omitted: int = 0  # items dropped to stay within the budget

    @property
    def ids(self) -> set[str]:
        return {i.id for i in self.items}

    def get(self, item_id: str) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "items": [i.to_dict() for i in self.items],
            "caveats": list(self.caveats),
            "omitted": self.omitted,
        }


# ---------------------------------------------------------------------------


def build(report: Report, *, budget: int = DEFAULT_BUDGET, mode: str = MODE_HISTORY) -> EvidencePack:
    """
    Number the report's facts.

    ``history`` mode: commits first (they are the backbone), then threads, then
    comments, then the target line ``S1`` so a "what" clause can still be grounded.
    ``code`` mode: ``S1`` (target line) and ``S2`` (enclosing block) first, then the
    same history items.
    """
    pack = EvidencePack(target=report.target.label, line=report.target.lines[0] if report.target.lines else "")
    code_items = _code_items(report, with_block=mode == MODE_CODE)

    commits = [s for s in reversed(report.steps) if not s.is_noise]  # oldest first
    if not commits:
        commits = list(reversed(report.steps))
    items: list[Item] = [_commit_item(report, s, f"C{n}") for n, s in enumerate(commits, 1)]

    thread_items: list[Item] = []
    comment_items: list[Item] = []
    pr_n = issue_n = comment_n = 0
    for thread in _threads_in_order(report, commits):
        if thread.kind == "pr":
            pr_n += 1
            tid = f"P{pr_n}"
        else:
            issue_n += 1
            tid = f"I{issue_n}"
        thread_items.append(_thread_item(thread, tid))
        for c in thread.comments:
            comment_n += 1
            comment_items.append(
                Item(
                    id=f"R{comment_n}",
                    kind="comment",
                    label=f"@{c.author} ({c.kind.replace('_', ' ')}) on {thread.kind.upper()} #{thread.number}",
                    text=f"@{c.author} {c.kind.replace('_', ' ')} on {tid}"
                    + (f" (file {c.path})" if c.path else "")
                    + f": {_clip(c.body, MAX_COMMENT_CHARS)}",
                    url=c.url,
                    quote=_clip(c.body, MAX_COMMENT_CHARS),
                )
            )

    ordered = code_items + items + thread_items + comment_items
    if mode != MODE_CODE:
        ordered = items + thread_items + comment_items + code_items
    used = 0
    for candidate in ordered:
        if used + len(candidate.text) > budget and pack.items:
            pack.omitted += 1
            continue
        pack.items.append(candidate)
        used += len(candidate.text)

    conf = report.confidence()
    if conf.level != "high":
        pack.caveats.append(f"history completeness is {conf.level}: " + "; ".join(conf.reasons))
    if not report.github.enabled:
        pack.caveats.append("pull request discussions were not fetched (run again with --github)")
    elif not report.threads:
        pack.caveats.append("no linked pull requests or issues were found for these commits")
    noise = len(report.steps) - len(commits)
    if noise > 0:
        pack.caveats.append(f"{noise} formatting/move/pass-through commits were left out")
    return pack


def format_for_prompt(pack: EvidencePack) -> str:
    lines = [f"Target: {pack.target}", f"Current text of the line: {pack.line.strip()}", ""]
    lines += [f"[{i.id}] {i.text}" for i in pack.items]
    if pack.caveats:
        lines += ["", "Caveats:"] + [f"- {c}" for c in pack.caveats]
    if pack.omitted:
        lines.append(f"- {pack.omitted} further items were omitted for length")
    return "\n".join(lines)


# ---------------------------------------------------------------------------


def _commit_item(report: Report, step: Step, item_id: str) -> Item:
    parts = [f'commit {step.short_sha} by {step.author} on {step.day}: "{step.subject.strip()}".']
    if step.is_origin:
        where = f" in {step.file_after}" if step.file_after else ""
        parts.append(f"The line was created here{where}.")
    elif step.is_deletion:
        parts.append("The line was deleted here.")
    if step.removed:
        parts.append("Removed: " + " | ".join(line.strip() for line in step.removed[:4]) + ".")
    if step.added:
        parts.append("Added: " + " | ".join(line.strip() for line in step.added[:4]) + ".")
    body = _clip(step.body.strip(), MAX_BODY_CHARS)
    if body:
        parts.append(f"Commit message: {body}")
    if step.landed_via:
        parts.append(f'Landed on the main branch via merge {step.landed_via.short_sha} "{step.landed_via.subject}".')
    refs = [str(r) for r in report.refs(step)]
    if refs:
        parts.append("References: " + ", ".join(refs) + ".")
    if step.found_by == "pickaxe":
        parts.append("(This commit is invisible to git blame.)")
    if step.shallow_boundary:
        parts.append("(Shallow-clone boundary: older history is unavailable.)")
    return Item(
        id=item_id,
        kind="commit",
        label=f'{step.short_sha} "{step.subject.strip()}" ({step.author}, {step.day})',
        text=" ".join(parts),
        url=report.commit_url(step.sha),
    )


def _code_items(report: Report, *, with_block: bool) -> list[Item]:
    """``S1``: the target line(s) exactly as at HEAD. ``S2``: the enclosing block (code questions only)."""
    t = report.target
    if t.text is not None or not t.lines:
        return []  # --text targets have no HEAD position
    head = report.repo.head
    blob = f"{report.web_base}/blob/{head}/{t.file}" if report.web_base and head else None
    span = f"{t.start}" if t.start == t.end else f"{t.start}-{t.end}"
    anchor = f"#L{t.start}" if t.start == t.end else f"#L{t.start}-L{t.end}"
    code = "\n".join(t.lines)
    items = [
        Item(
            id="S1",
            kind="code",
            label=f"{t.file}:{span} (HEAD)",
            text=f"source code, {t.file} line {span} at HEAD:\n{code}",
            url=f"{blob}{anchor}" if blob else None,
            quote=code,
        )
    ]
    if with_block and t.context and (t.context_start, t.context_end) != (t.start, t.end):
        block = _clip_lines("\n".join(t.context), MAX_CODE_CHARS)
        items.append(
            Item(
                id="S2",
                kind="code",
                label=f"{t.file} lines {t.context_start}-{t.context_end} (HEAD)",
                text=f"source code, {t.file} lines {t.context_start}-{t.context_end} at HEAD (enclosing block):\n"
                + block,
                url=f"{blob}#L{t.context_start}-L{t.context_end}" if blob else None,
                quote=block,
            )
        )
    return items


def _clip_lines(text: str, limit: int) -> str:
    """Clip at a line boundary (code must stay quotable line by line)."""
    if len(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        if used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept) + "\n…"


def _thread_item(thread: Thread, item_id: str) -> Item:
    name = "PR" if thread.kind == "pr" else "issue"
    parts = [f'{name} #{thread.number} "{thread.title}" ({thread.state}) opened by @{thread.author}.']
    if thread.labels:
        parts.append("Labels: " + ", ".join(thread.labels) + ".")
    if thread.linked_issues:
        parts.append("Linked issues: " + ", ".join(f"#{n}" for n in thread.linked_issues) + ".")
    body = _clip(thread.body.strip(), MAX_BODY_CHARS)
    if body:
        parts.append(f"Description: {body}")
    return Item(
        id=item_id,
        kind=thread.kind,
        label=f'{name} #{thread.number} "{thread.title}"',
        text=" ".join(parts),
        quote=body or thread.title,
        url=thread.url,
    )


def _threads_in_order(report: Report, commits: list[Step]) -> list[Thread]:
    """Threads in the order they are first referenced by a commit (oldest commit first)."""
    seen: list[int] = []
    for step in commits:
        for t in report.threads_for(step):
            if t.number not in seen:
                seen.append(t.number)
    for number in report.threads:  # anything fetched but not attached to a commit
        if number not in seen:
            seen.append(number)
    return [report.threads[n] for n in seen if n in report.threads]


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
