"""
history.py — talk to git and turn its output into Python objects.

The whole tool is built on one git command:

    git log -L <start>,<end>:<file>

which answers: "show me every commit that changed THESE LINES, following them
back through time (even across file renames)". For each commit git prints the
commit info plus a small diff showing how the lines changed.

Our job here is just to run that command and parse the result.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# We ask git to print each commit header on one line using a separator that
# will never appear in real commit messages. %x1f is the ASCII "unit separator".
SEP = "\x1f"
HEADER_PREFIX = "@@GITWHY@@"
# Fields: hash, short hash, author, author email, ISO date, subject, full body
FORMAT = f"{HEADER_PREFIX}{SEP}%H{SEP}%h{SEP}%an{SEP}%ae{SEP}%aI{SEP}%s{SEP}%b"


@dataclass
class Step:
    """One commit in the life of a line (or range of lines)."""

    sha: str
    short_sha: str
    author: str
    email: str
    date: str  # ISO 8601, e.g. 2023-11-19T10:22:01+00:00
    subject: str  # first line of commit message
    body: str  # the rest of the commit message
    file_before: str | None = None  # path before this commit (None if created)
    file_after: str | None = None  # path after this commit
    removed: list[str] = field(default_factory=list)  # lines taken away ("-")
    added: list[str] = field(default_factory=list)  # lines put in ("+")

    # These get filled in later by noise.py
    is_noise: bool = False
    noise_reason: str = ""

    @property
    def message(self) -> str:
        return (self.subject + "\n" + self.body).strip()

    @property
    def day(self) -> str:
        """Just the YYYY-MM-DD part of the date."""
        return self.date[:10]


class GitError(RuntimeError):
    pass


def run_git(args: list[str], cwd: Path) -> str:
    """Run a git command and return its stdout as text (raises on failure)."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def line_history(repo: Path, file: str, start: int, end: int) -> list[Step]:
    """
    Return the list of commits that touched lines start..end of `file`,
    newest first. This is the function everything else is built on.
    """
    out = run_git(
        [
            "log",
            f"-L{start},{end}:{file}",
            f"--format={FORMAT}",
            "--no-color",
        ],
        cwd=repo,
    )
    return _parse(out)


def _parse(text: str) -> list[Step]:
    """
    Walk through git's output line by line.

    The output looks like (simplified):

        @@GITWHY@@<sep>sha<sep>...<sep>subject<sep>body
        diff --git a/old.py b/new.py
        --- a/old.py
        +++ b/new.py
        @@ -8,1 +8,1 @@
        -    old line
        +    new line
        @@GITWHY@@<sep>...     <- next commit

    We create a new Step when we see a header, and attach the diff lines that
    follow to it.
    """
    steps: list[Step] = []
    current: Step | None = None
    in_body = False  # multi-line bodies continue until the diff starts

    for raw in text.splitlines():
        if raw.startswith(HEADER_PREFIX):
            parts = raw.split(SEP)
            # parts[0] is the prefix; the rest are our fields
            _, sha, short, author, email, date, subject, body = (parts + [""] * 8)[:8]
            current = Step(sha, short, author, email, date, subject, body)
            steps.append(current)
            in_body = True
            continue

        if current is None:
            continue  # junk before the first header

        if raw.startswith("diff --git"):
            in_body = False
            continue
        if raw.startswith("--- "):
            in_body = False
            current.file_before = _clean_path(raw[4:])
            continue
        if raw.startswith("+++ "):
            current.file_after = _clean_path(raw[4:])
            continue
        if raw.startswith("@@"):
            continue  # hunk header, we don't need the numbers
        if raw.startswith("-") and not in_body:
            current.removed.append(raw[1:])
            continue
        if raw.startswith("+") and not in_body:
            current.added.append(raw[1:])
            continue
        if in_body:
            # Git prints the body lines right after the header (because %b can
            # span multiple lines). Keep appending them.
            current.body = (current.body + "\n" + raw).strip("\n")

    return steps


def _clean_path(p: str) -> str | None:
    """Turn 'a/src/foo.py' into 'src/foo.py', and '/dev/null' into None."""
    p = p.strip()
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p
