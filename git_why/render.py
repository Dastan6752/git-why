"""
render.py — present a Report as rich terminal output, plain text, or JSON.

Renderers only format; every fact and judgement comes from ``report.py``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import IO, Any

from rich.console import Console
from rich.markup import escape

from .ask import MODE_LABEL, Answer, closing_line
from .history import Step
from .report import Report

KIND_BULLET = {"origin": "●", "change": "●", "deletion": "✕", "noise": "○"}
FOUND_BY_TAG = {
    "pickaxe": "invisible to git blame",
    "merge": "changed by the merge itself",
}


def _tag(report: Report, step: Step) -> str | None:
    if step.is_merge or step.found_by == "merge":
        return FOUND_BY_TAG["merge"]
    if step.found_by == "pickaxe" and report.target.text is not None:
        return None  # in --text mode everything comes from the pickaxe; nothing to compare against
    return FOUND_BY_TAG.get(step.found_by)


def _noun(report: Report) -> str:
    if report.target.function:
        return "function"
    return "line" if report.target.start == report.target.end else "lines"


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def render_json(report: Report, out: IO[str] | None = None, *, answer: Answer | None = None) -> None:
    out = out or sys.stdout
    data = report.to_dict()
    if answer is not None:
        data["ask"] = answer.to_dict()
    json.dump(data, out, indent=2, ensure_ascii=True)
    out.write("\n")


# ---------------------------------------------------------------------------
# Rich
# ---------------------------------------------------------------------------


def render_rich(
    report: Report,
    *,
    color: bool | None = None,
    max_body: int = 6,
    out: IO[str] | None = None,
    answer: Answer | None = None,
    show_removed: bool = False,
) -> None:
    out = out or sys.stdout
    console = Console(
        file=out, force_terminal=color if color is not None else None, no_color=color is False, soft_wrap=True
    )
    p = console.print

    p()
    p(f"  [bold]{escape(report.target.label)}[/bold]")
    for line in report.target.lines[:8]:
        p(f"  [dim]│[/dim] [cyan]{escape(line.rstrip())}[/cyan]")
    if len(report.target.lines) > 8:
        p(f"  [dim]│ … {len(report.target.lines) - 8} more lines[/dim]")
    p()

    first_origin = next((s for s in reversed(report.steps) if s.is_origin), None)
    single = report.target.start == report.target.end and not report.target.function
    shown: set[int] = set()
    for step in reversed(report.steps):
        _rich_step(
            report,
            step,
            p,
            reborn=single and step.is_origin and step is not first_origin,
            max_body=max_body,
            shown=shown,
        )

    p()
    p(f"  [bold]Summary[/bold] [dim](inferred)[/dim]: {escape(report.summary())}")
    conf = report.confidence()
    colour = {"high": "green", "medium": "yellow", "low": "red"}[conf.level]
    p(f"  [bold]Confidence:[/bold] [{colour}]{conf.level}[/{colour}]")
    for reason in conf.reasons:
        p(f"    [dim]- {escape(reason)}[/dim]")
    for w in report.warnings + report.github.warnings:
        p(f"  [yellow]warning:[/yellow] {escape(w)}")
    if answer is not None:
        _rich_answer(answer, p, show_removed)
    p()


def _rich_step(report: Report, step: Step, p, *, reborn: bool, max_body: int, shown: set[int]) -> None:  # type: ignore[no-untyped-def]
    tag = _tag(report, step)
    tag_md = f"  [bold red]⚡ {tag}[/bold red]" if tag else ""

    if step.is_noise:
        p(
            f"  [dim]○ {step.day}  {step.short_sha}  {escape(step.subject)}"
            f"   [italic]({escape(step.noise_reason)}, skipped)[/italic][/dim]"
        )
        return

    colour = {"origin": "green", "deletion": "red"}.get(step.kind, "yellow")
    committed = f" [dim](committed {step.committed_day})[/dim]" if step.committed_day else ""
    p(
        f"  [bold {colour}]{KIND_BULLET[step.kind]}[/bold {colour}] [bold]{step.day}[/bold]{committed}  "
        f'[magenta]{step.short_sha}[/magenta]  "{escape(step.subject.strip())}"  '
        f"[dim]({escape(step.author)})[/dim]{tag_md}"
    )
    if step.is_origin:
        where = f" [dim]in {escape(step.file_after)}[/dim]" if step.file_after else ""
        noun = _noun(report)
        verb = f"{noun} re-created here" if reborn else f"{noun} created here"
        if step.shallow_boundary:
            verb = "history starts here (shallow clone boundary)"
        p(f"  [dim]│[/dim]   [green]{verb}[/green]{where}")
    elif step.is_deletion:
        p("  [dim]│[/dim]   [red]line deleted here[/red] [dim](it came back later)[/dim]")
    for r in step.removed[:12]:
        p(f"  [dim]│[/dim]   [red]- {escape(r.rstrip())}[/red]")
    for a in step.added[:12]:
        p(f"  [dim]│[/dim]   [green]+ {escape(a.rstrip())}[/green]")
    if len(step.removed) > 12 or len(step.added) > 12:
        p(f"  [dim]│   … ({len(step.removed)} removed, {len(step.added)} added in total)[/dim]")

    body = step.body.strip()
    if body:
        lines = body.splitlines()
        for bl in lines[:max_body]:
            p(f"  [dim]│   {escape(bl)}[/dim]")
        if len(lines) > max_body:
            p(f"  [dim]│   … ({len(lines) - max_body} more lines)[/dim]")

    if step.landed_via is not None:
        lv = step.landed_via
        p(f'  [dim]│   landed via merge {lv.short_sha} "{escape(lv.subject.strip())}" on {lv.day}[/dim]')

    for ref in report.refs(step):
        p(f"  [dim]├─[/dim] [blue]{escape(str(ref))}[/blue]")

    for thread in report.threads_for(step):
        if thread.number in shown:
            kind = "PR" if thread.kind == "pr" else "issue"
            p(f"  [dim]│   {kind} #{thread.number} {escape(thread.title)} (shown above)[/dim]")
            continue
        shown.add(thread.number)
        _rich_thread(thread, p)

    strength = report.evidence_strength(step)
    url = report.commit_url(step.sha)
    tail = f"  [dim]evidence: {strength}" + (f"  {escape(url)}" if url else "") + "[/dim]"
    p(tail)


def _rich_thread(thread, p) -> None:  # type: ignore[no-untyped-def]
    kind = "PR" if thread.kind == "pr" else "issue"
    p(
        f"  [dim]│[/dim]   [bold blue]{kind} #{thread.number}[/bold blue] {escape(thread.title)} "
        f"[dim]({thread.state}, @{escape(thread.author)})[/dim]"
    )
    p(f"  [dim]│     {escape(thread.url)}[/dim]")
    if thread.body:
        for bl in [b for b in thread.body.splitlines() if b.strip()][:4]:
            p(f"  [dim]│[/dim]     [italic]{escape(bl)}[/italic]")
    if thread.linked_issues:
        p(f"  [dim]│     links: {', '.join('#' + str(n) for n in thread.linked_issues)}[/dim]")
    for c in thread.comments[:3]:
        loc = f" on {c.path}" if c.path else ""
        first = c.body.splitlines()[0] if c.body else ""
        p(f"  [dim]│[/dim]     [blue]@{escape(c.author)}[/blue] [dim]({c.kind}{escape(loc)})[/dim]: {escape(first)}")


# ---------------------------------------------------------------------------
# Plain text (no colour, no box drawing — safe for pipes and logs)
# ---------------------------------------------------------------------------


def render_plain(
    report: Report,
    *,
    max_body: int = 6,
    out: IO[str] | None = None,
    answer: Answer | None = None,
    show_removed: bool = False,
) -> None:
    out = out or sys.stdout
    w = out.write
    w(f"{report.target.label}\n")
    for line in report.target.lines[:8]:
        w(f"  | {line.rstrip()}\n")
    w("\n")

    first_origin = next((s for s in reversed(report.steps) if s.is_origin), None)
    single = report.target.start == report.target.end and not report.target.function
    shown: set[int] = set()
    for step in reversed(report.steps):
        reborn = single and step.is_origin and step is not first_origin
        if step.is_noise:
            w(f"o {step.day} {step.short_sha} {step.subject}  [{step.noise_reason}, skipped]\n")
            continue
        marker = {"origin": "*", "deletion": "x"}.get(step.kind, "*")
        tag = _tag(report, step)
        committed = f" (committed {step.committed_day})" if step.committed_day else ""
        w(
            f'{marker} {step.day}{committed} {step.short_sha} "{step.subject.strip()}" ({step.author})'
            + (f"  [{tag}]" if tag else "")
            + "\n"
        )
        if step.is_origin:
            noun = _noun(report)
            verb = (
                "history starts here (shallow clone boundary)"
                if step.shallow_boundary
                else (f"{noun} re-created here" if reborn else f"{noun} created here")
            )
            w(f"    {verb}" + (f" in {step.file_after}" if step.file_after else "") + "\n")
        elif step.is_deletion:
            w("    line deleted here (it came back later)\n")
        for r in step.removed[:12]:
            w(f"    - {r.rstrip()}\n")
        for a in step.added[:12]:
            w(f"    + {a.rstrip()}\n")
        body = step.body.strip()
        if body:
            for bl in body.splitlines()[:max_body]:
                w(f"    {bl}\n")
        if step.landed_via is not None:
            lv = step.landed_via
            w(f'    landed via merge {lv.short_sha} "{lv.subject.strip()}" on {lv.day}\n')
        for ref in report.refs(step):
            w(f"    -> {ref}\n")
        for t in report.threads_for(step):
            kind = "PR" if t.kind == "pr" else "issue"
            if t.number in shown:
                w(f"    {kind} #{t.number}: {t.title} (shown above)\n")
                continue
            shown.add(t.number)
            w(f"    {kind} #{t.number}: {t.title} ({t.state}, @{t.author}) {t.url}\n")
            if t.body:
                w(f"      {t.body.splitlines()[0]}\n")
            for c in t.comments[:3]:
                first = c.body.splitlines()[0] if c.body else ""
                w(f"      @{c.author} ({c.kind}): {first}\n")
        url = report.commit_url(step.sha)
        w(f"    evidence: {report.evidence_strength(step)}" + (f"  {url}" if url else "") + "\n")

    w("\n")
    w(f"Summary (inferred): {report.summary()}\n")
    conf = report.confidence()
    w(f"Confidence: {conf.level}\n")
    for reason in conf.reasons:
        w(f"  - {reason}\n")
    for warning in report.warnings + report.github.warnings:
        w(f"warning: {warning}\n")
    if answer is not None:
        _plain_answer(answer, w, show_removed)


# ---------------------------------------------------------------------------
# --ask section (always last, always the same shape)
# ---------------------------------------------------------------------------


def _answer_headline(answer: Answer) -> str:
    if answer.status == "answered":
        return f"AI answer (model {answer.model} via local Ollama at {answer.host}; generated text - check the sources)"
    if answer.status == "insufficient_evidence":
        return f"AI answer (model {answer.model}): the gathered evidence does not answer this question"
    if answer.status == "no_model_selected":
        return "AI answer: no model selected"
    return "AI answer: unavailable"


def _removed_headline(answer: Answer, show_removed: bool) -> str:
    n = len(answer.removed)
    plural = "s" if n != 1 else ""
    if show_removed:
        return f"Removed {n} sentence{plural} the evidence does not support:"
    return (
        f"Removed {n} generated sentence{plural} the evidence does not support (--show-removed or --json lists them)."
    )


def _plain_answer(answer: Answer, w: Callable[[str], Any], show_removed: bool = False) -> None:
    w("\n")
    w(f"Question: {answer.question}\n")
    w(_answer_headline(answer) + "\n")
    w(f"  question type: {MODE_LABEL.get(answer.mode, answer.mode)}\n")
    if answer.note:
        w(f"  note: {answer.note}\n")
    if answer.message:
        w(f"  {answer.message}\n")
    if answer.fallback:
        fb = answer.fallback
        kind = "code" if fb.item.kind == "code" else "comment"
        w(f"  Most relevant {kind} in the evidence (exact quote) [{fb.item.id}]:\n")
        w(f'    "{fb.quote}"\n')
        w(f"    - {fb.item.label}" + (f"  {fb.item.url}" if fb.item.url else "") + "\n")
        w(f"  {closing_line(answer.mode)}\n")
    for s in answer.sentences:
        cite = f" [{', '.join(s.citations)}]" if s.citations else ""
        w(f"  {s.text}{cite}\n")
    if answer.sources:
        w("  Sources\n")
        for item in answer.sources:
            w(f"    [{item.id}] {item.label}" + (f"  {item.url}" if item.url else "") + "\n")
    if answer.removed:
        w(f"  {_removed_headline(answer, show_removed)}\n")
        if show_removed:
            for r in answer.removed:
                w(f"    - {r.text}\n      reason: {r.reason}\n")
    if answer.setup:
        w("  The Git evidence above is complete without AI. To get answers:\n")
        for step in answer.setup:
            w(f"    {step}\n")


def _rich_answer(answer: Answer, p, show_removed: bool = False) -> None:  # type: ignore[no-untyped-def]
    p()
    p(f"  [bold]Question:[/bold] {escape(answer.question)}")
    colour = {"answered": "green", "insufficient_evidence": "yellow"}.get(answer.status, "yellow")
    p(f"  [bold {colour}]{escape(_answer_headline(answer))}[/bold {colour}]")
    p(f"    [dim]question type: {MODE_LABEL.get(answer.mode, answer.mode)}[/dim]")
    if answer.note:
        p(f"    [dim]note: {escape(answer.note)}[/dim]")
    if answer.message:
        p(f"    {escape(answer.message)}")
    if answer.fallback:
        fb = answer.fallback
        kind = "code" if fb.item.kind == "code" else "comment"
        p(f"    Most relevant {kind} in the evidence (exact quote) [magenta]\\[{fb.item.id}][/magenta]:")
        p(f'      [italic]"{escape(fb.quote)}"[/italic]')
        url = f"  [dim]{escape(fb.item.url)}[/dim]" if fb.item.url else ""
        p(f"      - {escape(fb.item.label)}{url}")
        p(f"    [bold]{closing_line(answer.mode)}[/bold]")
    for s in answer.sentences:
        cite = f" [magenta]\\[{', '.join(s.citations)}][/magenta]" if s.citations else ""
        p(f"    {escape(s.text)}{cite}")
    if answer.sources:
        p("    [bold]Sources[/bold]")
        for item in answer.sources:
            url = f"  [dim]{escape(item.url)}[/dim]" if item.url else ""
            p(f"      [magenta]\\[{item.id}][/magenta] {escape(item.label)}{url}")
    if answer.removed:
        p(f"    [yellow]{escape(_removed_headline(answer, show_removed))}[/yellow]")
        if show_removed:
            for r in answer.removed:
                p(f"      [dim]- {escape(r.text)}[/dim]")
                p(f"        [dim]reason: {escape(r.reason)}[/dim]")
    if answer.setup:
        p("    [dim]The Git evidence above is complete without AI. To get answers:[/dim]")
        for step in answer.setup:
            p(f"      {escape(step)}")
