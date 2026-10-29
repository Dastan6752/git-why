"""
render.py — print the timeline nicely in the terminal using `rich`.
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from .history import Step
from .refs import refs_for

console = Console()


def render(file: str, start: int, end: int, current_lines: list[str], steps: list[Step]) -> None:
    where = f"{file}:{start}" if start == end else f"{file}:{start}-{end}"
    console.print()
    console.print(f"  [bold]{where}[/bold]")
    for line in current_lines:
        console.print(f"  [dim]│[/dim] [cyan]{line.rstrip()}[/cyan]")
    console.print()

    # git gives newest first; a story reads better oldest first.
    for step in reversed(steps):
        _render_step(step)

    console.print()
    _render_summary(steps)
    console.print()


def _render_step(step: Step) -> None:
    refs = refs_for(step)

    if step.is_noise:
        bullet = "[dim]○[/dim]"
        line = Text.assemble(
            (f"{step.day}  ", "dim"),
            (f"{step.short_sha}  ", "dim"),
            (f"{step.subject}", "dim"),
            (f"   ({step.noise_reason}, skipped)", "dim italic"),
        )
        console.print(f"  {bullet} ", end="")
        console.print(line)
        return

    origin = step.file_before is None
    bullet = "[bold green]●[/bold green]" if origin else "[bold yellow]●[/bold yellow]"
    console.print(
        f"  {bullet} [bold]{step.day}[/bold]  [magenta]{step.short_sha}[/magenta]  "
        f"{_quote(step.subject)}  [dim]({step.author})[/dim]"
    )
    if origin:
        console.print(f"  [dim]│[/dim]   [green]line created here[/green]"
                      + (f" [dim]in {step.file_after}[/dim]" if step.file_after else ""))
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

    console.print("  [bold]Summary:[/bold] " + "; ".join(parts) + ".")


def _quote(s: str) -> str:
    return f'"{s.strip()}"'
