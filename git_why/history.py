"""
history.py — talk to git and turn its output into Python objects.

Two git commands power the tool:

    git log -L <start>,<end>:<file>          (line-range tracking)
    git log -G <regex> --follow -p -- <file> (the "pickaxe": search diffs)

Both print commit headers followed by diffs, so one parser handles both.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# We ask git to print each commit header on one line using a separator that
# will never appear in real commit messages. \x1f is the ASCII "unit separator".
SEP = "\x1f"
HEADER_PREFIX = "@@GITWHY@@"
# Fields: hash, short hash, author, email, ISO date, unix timestamp, subject, body
FORMAT = f"{HEADER_PREFIX}{SEP}%H{SEP}%h{SEP}%an{SEP}%ae{SEP}%aI{SEP}%ct{SEP}%s{SEP}%b"


@dataclass
class Hunk:
    """One '@@ ... @@' block of a diff: the lines removed and added together."""

    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)


@dataclass
class Step:
    """One commit in the life of a line (or range of lines)."""

    sha: str
    short_sha: str
    author: str
    email: str
    date: str  # ISO 8601, e.g. 2023-11-19T10:22:01+00:00
    timestamp: int  # unix commit time, used for sorting
    subject: str  # first line of commit message
    body: str  # the rest of the commit message
    file_before: str | None = None  # path before this commit (None if created)
    file_after: str | None = None  # path after this commit
    hunks: list[Hunk] = field(default_factory=list)

    # Which strategy discovered this commit: "range" (git log -L) or "pickaxe".
    found_by: str = "range"

    # Filled in later by noise.py
    is_noise: bool = False
    noise_reason: str = ""

    # --- convenience views -------------------------------------------------

    @property
    def removed(self) -> list[str]:
        return [l for h in self.hunks for l in h.removed]

    @property
    def added(self) -> list[str]:
        return [l for h in self.hunks for l in h.added]

    @property
    def is_origin(self) -> bool:
        """True if the line(s) were brand new in this commit (nothing replaced)."""
        return bool(self.added) and not self.removed

    @property
    def is_deletion(self) -> bool:
        """True if this commit removed the line(s) without replacing them."""
        return bool(self.removed) and not self.added

    @property
    def message(self) -> str:
        return (self.subject + "\n" + self.body).strip()

    @property
    def day(self) -> str:
        return self.date[:10]

    def focus(self, removed: list[str], added: list[str]) -> None:
        """Replace all hunks with a single hunk containing only the lines we care about."""
        self.hunks = [Hunk(removed=list(removed), added=list(added))]


class GitError(RuntimeError):
    pass


def run_git(args: list[str], cwd: Path) -> str:
    """Run a git command and return its stdout as text (raises on failure)."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


# ---------------------------------------------------------------------------
# Strategy 1: git log -L
# ---------------------------------------------------------------------------

def line_history(repo: Path, file: str, start: int, end: int) -> list[Step]:
    """Commits that touched lines start..end of `file`, newest first."""
    out = run_git(
        ["log", f"-L{start},{end}:{file}", f"--format={FORMAT}", "--no-color"],
        cwd=repo,
    )
    return parse_log(out)


# ---------------------------------------------------------------------------
# Strategy 2: git log -G (pickaxe) — see pickaxe.py for the walking logic
# ---------------------------------------------------------------------------

def pickaxe_search(repo: Path, file: str, regex: str) -> list[Step]:
    """
    Commits whose diff of `file` has a +/- line matching `regex`, newest first.
    --follow makes git keep tracking the file across renames.
    -U0 means "no context lines" so hunks contain only real changes.
    """
    out = run_git(
        [
            "log",
            f"-G{regex}",
            "--follow",
            "-p",
            "-U0",
            "-M",
            "--no-color",
            f"--format={FORMAT}",
            "--",
            file,
        ],
        cwd=repo,
    )
    return parse_log(out)


# ---------------------------------------------------------------------------
# Parser shared by both strategies
# ---------------------------------------------------------------------------

def parse_log(text: str) -> list[Step]:
    """
    Walk through git's output line by line.

        @@GITWHY@@<sep>sha<sep>...<sep>subject<sep>body
        more body lines...
        diff --git a/old.py b/new.py
        --- a/old.py
        +++ b/new.py
        @@ -8,1 +8,1 @@
        -    old line
        +    new line
        @@GITWHY@@<sep>...     <- next commit
    """
    steps: list[Step] = []
    current: Step | None = None
    hunk: Hunk | None = None
    in_body = False  # body lines continue until the first "diff --git"

    for raw in text.splitlines():
        if raw.startswith(HEADER_PREFIX):
            parts = raw.split(SEP)
            _, sha, short, author, email, date, ts, subject, body = (parts + [""] * 9)[:9]
            current = Step(sha, short, author, email, date, int(ts or 0), subject, body)
            steps.append(current)
            hunk = None
            in_body = True
            continue

        if current is None:
            continue  # junk before the first header

        if raw.startswith("diff --git"):
            in_body = False
            hunk = None
            continue

        if in_body:
            # Git prints body lines right after the header (because %b can span
            # multiple lines). Keep appending them. Bullet lists starting with
            # "-" are safe here because we check in_body first.
            current.body = (current.body + "\n" + raw).strip("\n")
            continue

        if raw.startswith("--- "):
            current.file_before = _clean_path(raw[4:])
        elif raw.startswith("+++ "):
            current.file_after = _clean_path(raw[4:])
        elif raw.startswith("@@"):
            hunk = Hunk()
            current.hunks.append(hunk)
        elif raw.startswith("-") and hunk is not None:
            hunk.removed.append(raw[1:])
        elif raw.startswith("+") and hunk is not None:
            hunk.added.append(raw[1:])
        # anything else (index lines, "\ No newline at end of file", ...) is ignored

    return steps


def _clean_path(p: str) -> str | None:
    """Turn 'a/src/foo.py' into 'src/foo.py', and '/dev/null' into None."""
    p = p.strip()
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p
