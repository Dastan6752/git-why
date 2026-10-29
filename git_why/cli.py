"""
cli.py — the ``git why`` command.

Exit codes
----------
0  success (a report was produced)
1  usage / input error: bad target syntax, missing file, ambiguous function name
2  git error: not a repository, file not tracked, binary file, git failure
3  no history: the target is valid but no commit touches it

GitHub problems (offline, rate limit, private repo) never change the exit code;
they are reported as warnings and the local evidence is still shown.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import typer

from . import __version__
from .core import EXIT_GIT, EXIT_NO_HISTORY, EXIT_USAGE, Options, UserError, investigate
from .history import GitError
from .render import render_json, render_plain, render_rich

DESCRIPTION = (
    "Answer [bold]why is this line here?[/bold] by reconstructing its decision history "
    "from git — across renames, formatter runs and side branches that git blame cannot see."
)

# Shown after the options table. Each example is its own paragraph so Rich keeps
# the lines apart; keep these in sync with README "Usage".
EXAMPLES = (
    "[bold]Examples[/bold]\n\n"
    "  git why src/app.py:42                      one line\n\n"
    "  git why src/app.py:40-55                   a range\n\n"
    "  git why src/app.py --function retry        a whole function\n\n"
    "  git why src/app.py:42 --github             with linked issues, PRs, reviews\n\n"
    "  git why src/app.py:42 --merges             include edits made by merges\n\n"
    "  git why src/app.py --text 'TIMEOUT = 30'   a line that no longer exists\n\n"
    "  git why src/app.py:42 --json               machine-readable output\n\n"
    "Set GITHUB_TOKEN (or GH_TOKEN) to raise the GitHub API rate limit and reach private repos. "
    "Exit codes: 0 ok, 1 usage error, 2 git error, 3 no history found."
)

# A single-command app: the command's own help/epilog is what `--help` shows
# (the Typer(help=...) text is only used for multi-command groups).
app = typer.Typer(
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_TARGET = re.compile(r"^(?P<file>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$")


def parse_target(target: str) -> tuple[str, int | None, int | None]:
    """'f.py:42' -> ('f.py', 42, 42); 'f.py:3-9' -> ('f.py', 3, 9); 'f.py' -> ('f.py', None, None)."""
    m = _TARGET.match(target)
    if not m:
        return target, None, None
    start = int(m["start"])
    end = int(m["end"]) if m["end"] else start
    if end < start:
        start, end = end, start
    return m["file"], start, end


def _version(value: bool) -> None:
    if value:
        typer.echo(f"git-why {__version__}")
        raise typer.Exit()


def _fail(message: str, code: int, hint: str | None = None) -> None:
    typer.secho(f"error: {message}", fg="red", err=True)
    if hint:
        typer.secho(f"hint: {hint}", fg="yellow", err=True)
    raise typer.Exit(code)


@app.command(name="git-why", help=DESCRIPTION, epilog=EXAMPLES)
def main(
    target: str = typer.Argument(
        ..., help="FILE:LINE, FILE:START-END, or FILE (with --function / --text)", metavar="TARGET"
    ),
    function: str | None = typer.Option(
        None, "--function", "-f", help="Track a whole function by name (git-native -L :funcname:)"
    ),
    text: str | None = typer.Option(
        None, "--text", "-t", help="Track a line by its exact text — works for lines that no longer exist"
    ),
    github: bool = typer.Option(
        False, "--github", "-g", help="Fetch linked issues, PRs and review comments from GitHub"
    ),
    github_repo: str | None = typer.Option(
        None, "--github-repo", help="OWNER/NAME to use instead of the origin remote", metavar="OWNER/NAME"
    ),
    merges: bool = typer.Option(
        False, "--merges", "-m", help="Also inspect merge commits for changes made during conflict resolution"
    ),
    fmt: str = typer.Option("auto", "--format", "-F", help="auto | rich | plain | json", metavar="FORMAT"),
    json_flag: bool = typer.Option(False, "--json", help="Shortcut for --format json"),
    show_all: bool = typer.Option(False, "--all", "-a", help="Do not classify formatting/move commits as noise"),
    no_pickaxe: bool = typer.Option(
        False, "--no-pickaxe", help="Only use git log -L (faster; may miss side-branch changes)"
    ),
    no_color: bool = typer.Option(False, "--no-color", help="Disable colours in rich output"),
    max_body: int = typer.Option(6, "--max-body", help="Commit-message lines shown per commit", min=0),
    debug: bool = typer.Option(False, "--debug", help="Print search statistics to stderr (never prints secrets)"),
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version, is_eager=True, help="Show version and exit"
    ),
) -> None:
    file, start, end = parse_target(target)
    if function and text:
        _fail("--function and --text are mutually exclusive", EXIT_USAGE)
    if (function or text) and start is not None:
        _fail("give just the FILE (no :LINE) when using --function or --text", EXIT_USAGE)
    if not function and not text and start is None:
        _fail(
            "expected FILE:LINE or FILE:START-END, e.g. src/app.py:42",
            EXIT_USAGE,
            hint="or use --function NAME / --text 'exact line'",
        )
    if github_repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", github_repo):
        _fail("--github-repo must look like OWNER/NAME", EXIT_USAGE)

    output = "json" if json_flag else fmt
    if output not in ("auto", "rich", "plain", "json"):
        _fail(f"unknown format {output!r}; use auto, rich, plain or json", EXIT_USAGE)
    if output == "auto":
        output = "rich" if sys.stdout.isatty() else "plain"

    opts = Options(
        function=function,
        text=text,
        pickaxe=not no_pickaxe,
        merges=merges,
        github=github or bool(github_repo),
        github_repo=github_repo,
        classify_noise=not show_all,
        debug=(lambda m: typer.secho(f"[debug] {m}", fg="yellow", err=True)) if debug else (lambda _m: None),
    )

    try:
        report = investigate(Path(file), start, end, opts)
    except UserError as exc:
        _fail(str(exc), exc.exit_code, exc.hint)
        return  # pragma: no cover - _fail raises
    except GitError as exc:
        _fail(f"git failed: {exc}", EXIT_GIT)
        return  # pragma: no cover

    if output == "json":
        render_json(report)
    elif output == "plain":
        render_plain(report, max_body=max_body)
    else:
        render_rich(report, color=False if no_color else None, max_body=max_body)

    if not report.steps:
        typer.secho("no history found for this target", fg="yellow", err=True)
        raise typer.Exit(EXIT_NO_HISTORY)


if __name__ == "__main__":  # pragma: no cover
    app()
