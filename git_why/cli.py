"""
cli.py — the command-line interface.

Usage:
    git why src/payments/retry.py:42        # one line
    git why src/payments/retry.py:40-45     # a range
    git why src/payments/retry.py:42 --all  # also show noise commits in full
"""

from __future__ import annotations

import re
from pathlib import Path

import typer

from . import history, noise
from .render import render

app = typer.Typer(add_completion=False, help="Answer: why is this line here?")

_TARGET = re.compile(r"^(?P<file>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$")


def parse_target(target: str) -> tuple[str, int, int]:
    """'path/file.py:42' -> ('path/file.py', 42, 42); 'f.py:3-9' -> ('f.py', 3, 9)"""
    m = _TARGET.match(target)
    if not m:
        raise typer.BadParameter("expected FILE:LINE or FILE:START-END, e.g. src/app.py:42")
    start = int(m["start"])
    end = int(m["end"]) if m["end"] else start
    if end < start:
        start, end = end, start
    return m["file"], start, end


def find_repo_root(path: Path) -> Path:
    return Path(history.run_git(["rev-parse", "--show-toplevel"], cwd=path).strip())


@app.command()
def main(
    target: str = typer.Argument(..., help="FILE:LINE or FILE:START-END"),
    show_noise: bool = typer.Option(False, "--all", "-a", help="Treat every commit as meaningful"),
) -> None:
    file, start, end = parse_target(target)

    path = Path(file).resolve()
    if not path.exists():
        typer.secho(f"error: {file} does not exist", fg="red", err=True)
        raise typer.Exit(1)

    try:
        repo = find_repo_root(path.parent)
    except history.GitError:
        typer.secho(f"error: {file} is not inside a git repository", fg="red", err=True)
        raise typer.Exit(1)

    # git wants the path relative to the repo root
    rel = path.relative_to(repo).as_posix()

    try:
        steps = history.line_history(repo, rel, start, end)
    except history.GitError as e:
        typer.secho(f"git error: {e}", fg="red", err=True)
        raise typer.Exit(1)

    if not show_noise:
        steps = noise.classify_all(steps)

    current = path.read_text(errors="replace").splitlines()[start - 1 : end]
    render(rel, start, end, current, steps)


if __name__ == "__main__":
    app()
