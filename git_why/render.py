"""
render.py — print the timeline nicely in the terminal using `rich`.
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from .history import Step
from .refs import refs_for

console = Console()

HIDDEN_TAG = "[bold red]⚡ invisible to git blame[/bold red]"


def render(file: str, start: int, end: int, current_lines: list[str], steps: list[Step]) -> None:
    where = f"{file}:{start}" if start == end else f"{file}:{start}-{end}"
    console.print()
    console.print(f"  [bold]{where}[/bold]")
    for line in current_lines:
        console.print(f"  [dim]│[/dim] [cyan]{line.rstrip()}[/cyan]")
    console.print()

    # git gives newest first; a story reads better oldest first.
    first_origin = next((s for s in reversed(steps) if s.is_origin), None)
    single = start == end
    for step in reversed(steps):
        # "re-created" is only meaningful for one line; in a range, different
        # lines simply have different birthdays.
        _render_step(step, reborn=single and step.is_origin and step is not first_origin)

    console.print()
    _render_summary(steps)
    console.print()


def _render_step(step: Step, reborn: bool = False) -> None:
    refs = refs_for(step)
    hidden = f"  {HIDDEN_TAG}" if step.found_by == "pickaxe" else ""

    if step.is_noise:
        line = Text.assemble(
            (f"{step.day}  ", "dim"),
            (f"{step.short_sha}  ", "dim"),
            (f"{step.subject}", "dim"),
            (f"   ({step.noise_reason}, skipped)", "dim italic"),
        )
        console.print("  [dim]○[/dim] ", end="")
        console.print(line)
        return

    if step.is_origin:
        bullet = "[bold green]●[/bold green]"
    elif step.is_deletion:
        bullet = "[bold red]✕[/bold red]"
    else:
        bullet = "[bold yellow]●[/bold yellow]"
    console.print(
        f"  {bullet} [bold]{step.day}[/bold]  [magenta]{step.short_sha}[/magenta]  "
        f"{_quote(step.subject)}  [dim]({step.author})[/dim]{hidden}"
    )
    if step.is_origin:
        where = f" [dim]in {step.file_after}[/dim]" if step.file_after else ""
        verb = "line re-created here" if reborn else "line created here"
        console.print(f"  [dim]│[/dim]   [green]{verb}[/green]{where}")
        for a in step.added:
            console.print(f"  [dim]│[/dim]   [green]+ {a.rstrip()}[/green]")
    elif step.is_deletion:
        console.print(f"  [dim]│[/dim]   [red]line deleted here[/red] [dim](it came back later)[/dim]")
        for r in step.removed:
            console.print(f"  [dim]│[/dim]   [red]- {r.rstrip()}[/red]")
    else:
        for r in step.removed:
            console.print(f"  [dim]│[/dim]   [red]- {r.rstrip()}[/red]")
        for a in step.added:
            console.print(f"  [dim]│[/dim]   [green]+ {a.rstrip()}[/green]")

    # The commit body is where the "why" usually lives — show it.
    body = step.body.strip()
    if body:
        for bl in body.splitlines()[:6]:  # don't flood the screen
            console.print(f"  [dim]│[/dim]   [dim]{bl}[/dim]")

    for r in refs:
        console.print(f"  [dim]└─[/dim] [blue]{r}[/blue]")


def _render_summary(steps: list[Step]) -> None:
    """A plain-English recap. (Later: optional LLM-written summary.)"""
    real = [s for s in steps if not s.is_noise]
    if not real:
        console.print("  [dim]No meaningful history found.[/dim]")
        return
    origin = real[-1]  # oldest real commit
    changes = real[:-1]  # newer real commits
    noise = len(steps) - len(real)
    hidden = sum(1 for s in steps if s.found_by == "pickaxe")

    parts = [f"Introduced on {origin.day} by {origin.author} ({_quote(origin.subject)})"]
    o_refs = refs_for(origin)
    if o_refs:
        parts[-1] += f" — linked to {', '.join(str(r) for r in o_refs)}"
    if changes:
        parts.append(
            f"changed meaningfully {len(changes)} time{'s' if len(changes) != 1 else ''} since, "
            f"most recently on {changes[0].day} by {changes[0].author}"
        )
    if noise:
        parts.append(f"{noise} formatting/move commit{'s' if noise != 1 else ''} ignored")
    if hidden:
        parts.append(
            f"[bold red]{hidden} commit{'s' if hidden != 1 else ''} recovered that git blame / git log -L cannot see[/bold red]"
        )

    console.print("  [bold]Summary:[/bold] " + "; ".join(parts) + ".")


def _quote(s: str) -> str:
    return f'"{s.strip()}"'
