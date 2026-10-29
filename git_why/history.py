"""
history.py — everything that talks to ``git`` and turns its output into Python objects.

Three git commands power the tool:

    git log -L <start>,<end>:<file>              line-range tracking
    git log -L :<regex>:<file>                   function tracking (git-native)
    git log -G <regex> --follow -p -- <file>     the "pickaxe": search diffs

All of them print commit headers followed by diffs, so one parser handles all.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Each commit header is printed on one line using a separator that never appears
# in commit messages. \x1f is the ASCII "unit separator".
SEP = "\x1f"
HEADER_PREFIX = "@@GITWHY@@"
# Fields: hash, short hash, author, email, ISO date, unix timestamp, parents, subject, body
FORMAT = f"{HEADER_PREFIX}{SEP}%H{SEP}%h{SEP}%an{SEP}%ae{SEP}%aI{SEP}%ct{SEP}%P{SEP}%s{SEP}%b"

GIT_TIMEOUT = 120  # seconds per git invocation; protects against pathological repos


class GitError(RuntimeError):
    """A git command failed. ``stderr`` is in the message."""


class NotARepositoryError(GitError):
    pass


@dataclass
class Hunk:
    """One ``@@ ... @@`` block of a diff: the lines removed and added together."""

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
    subject: str  # first line of the commit message
    body: str  # the rest of the commit message
    parents: list[str] = field(default_factory=list)
    file_before: str | None = None  # path before this commit (None if created)
    file_after: str | None = None  # path after this commit (None if deleted)
    hunks: list[Hunk] = field(default_factory=list)

    # Which strategy discovered this commit: "range" (git log -L), "pickaxe", "merge".
    found_by: str = "range"

    # Filled in later by noise.py
    is_noise: bool = False
    noise_reason: str = ""

    # Filled in by merges.py: the merge commit that brought this change onto the
    # current branch's first-parent history (None if it is on the mainline).
    landed_via: Step | None = None

    # True if this commit is a shallow-clone boundary: history is cut here.
    shallow_boundary: bool = False

    # --- convenience views -------------------------------------------------

    @property
    def removed(self) -> list[str]:
        return [line for h in self.hunks for line in h.removed]

    @property
    def added(self) -> list[str]:
        return [line for h in self.hunks for line in h.added]

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def is_origin(self) -> bool:
        """True if the line(s) were brand new in this commit (nothing replaced)."""
        return bool(self.added) and not self.removed

    @property
    def is_deletion(self) -> bool:
        """True if this commit removed the line(s) without replacing them."""
        return bool(self.removed) and not self.added

    @property
    def kind(self) -> str:
        if self.is_noise:
            return "noise"
        if self.is_origin:
            return "origin"
        if self.is_deletion:
            return "deletion"
        return "change"

    @property
    def message(self) -> str:
        return (self.subject + "\n" + self.body).strip()

    @property
    def day(self) -> str:
        return self.date[:10]

    def focus(self, removed: list[str], added: list[str]) -> None:
        """Replace all hunks with a single hunk containing only the lines we care about."""
        self.hunks = [Hunk(removed=list(removed), added=list(added))]


@dataclass
class RepoInfo:
    root: Path
    head: str
    shallow: bool
    shallow_boundaries: set[str]
    remote_url: str | None


# ---------------------------------------------------------------------------
# Running git
# ---------------------------------------------------------------------------


def run_git(args: list[str], cwd: Path, *, config: dict[str, str] | None = None) -> str:
    """Run a git command and return stdout as text. Raises GitError on failure."""
    cmd = ["git"]
    for key, value in (config or {}).items():
        cmd += ["-c", f"{key}={value}"]
    cmd += args
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C.UTF-8")
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            env=env,
        )
    except FileNotFoundError as exc:  # git itself is missing
        raise GitError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args[:2])} timed out after {GIT_TIMEOUT}s") from exc
    if result.returncode != 0:
        msg = result.stderr.strip() or f"git {' '.join(args)} failed"
        if "not a git repository" in msg:
            raise NotARepositoryError(msg)
        raise GitError(msg)
    return result.stdout


def repo_info(path: Path) -> RepoInfo:
    """Collect facts about the repository containing ``path``."""
    root = Path(run_git(["rev-parse", "--show-toplevel"], cwd=path).strip())
    try:
        head = run_git(["rev-parse", "HEAD"], cwd=root).strip()
    except GitError as exc:
        raise GitError("repository has no commits yet") from exc
    shallow = run_git(["rev-parse", "--is-shallow-repository"], cwd=root).strip() == "true"
    boundaries: set[str] = set()
    if shallow:
        git_dir = Path(run_git(["rev-parse", "--git-dir"], cwd=root).strip())
        if not git_dir.is_absolute():
            git_dir = root / git_dir
        shallow_file = git_dir / "shallow"
        if shallow_file.exists():
            boundaries = {ln.strip() for ln in shallow_file.read_text().splitlines() if ln.strip()}
    remote: str | None
    try:
        remote = run_git(["remote", "get-url", "origin"], cwd=root).strip() or None
    except GitError:
        remote = None
    return RepoInfo(root, head, shallow, boundaries, remote)


def head_content(repo: Path, rel_path: str) -> str | None:
    """File content at HEAD, or None if the file is not tracked at HEAD."""
    try:
        return run_git(["show", f"HEAD:{rel_path}"], cwd=repo)
    except GitError:
        return None


def is_binary(repo: Path, rel_path: str) -> bool:
    """Same heuristic git uses: a NUL byte in the first 8000 bytes means binary."""
    try:
        result = subprocess.run(
            ["git", "cat-file", "blob", f"HEAD:{rel_path}"],
            cwd=repo,
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return b"\0" in result.stdout[:8000]


def worktree_differs(repo: Path, rel_path: str) -> bool:
    """True if the working tree copy differs from HEAD (line numbers may be off)."""
    try:
        run_git(["diff", "--quiet", "HEAD", "--", rel_path], cwd=repo)
        return False
    except GitError:
        return True


# ---------------------------------------------------------------------------
# Strategy 1: git log -L  (lines or functions)
# ---------------------------------------------------------------------------


def line_history(repo: Path, file: str, start: int, end: int) -> list[Step]:
    """Commits that touched lines start..end of ``file``, newest first."""
    out = run_git(["log", f"-L{start},{end}:{file}", f"--format={FORMAT}", "--no-color"], cwd=repo)
    return parse_log(out)


def _bre_escape(text: str) -> str:
    """Escape for POSIX *basic* regex, which is what ``git log -L :<regex>:`` uses."""
    return re.sub(r"([\\.\[\]*^$])", r"\\\1", text)


def funcname_regex(name: str) -> str:
    """BRE that matches a function-header line naming ``name`` (not ``widget`` for ``get``)."""
    escaped = _bre_escape(name)
    return rf"\(^\|[^A-Za-z0-9_]\){escaped}\($\|[^A-Za-z0-9_]\)"


def function_history(repo: Path, file: str, name: str, diff_driver: str | None) -> list[Step]:
    """
    Git-native function tracking: ``git log -L :<regex>:<file>``.

    Git decides what a "function header" is via the file's diff driver. Repos
    rarely configure one, in which case only column-0 lines count and e.g.
    Python methods are invisible. We therefore activate git's *built-in* driver
    for the language (``diff=python`` etc.) through a temporary attributes
    file. A repo's own ``.gitattributes`` still takes precedence.
    """
    config: dict[str, str] = {}
    tmp: str | None = None
    try:
        if diff_driver:
            fd, tmp = tempfile.mkstemp(prefix="git-why-attrs-")
            with os.fdopen(fd, "w") as fh:
                fh.write(f"{_attr_pattern(file)} diff={diff_driver}\n")
            config["core.attributesFile"] = tmp
        out = run_git(
            ["log", f"-L:{funcname_regex(name)}:{file}", f"--format={FORMAT}", "--no-color"],
            cwd=repo,
            config=config,
        )
    finally:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    return parse_log(out)


def _attr_pattern(file: str) -> str:
    """gitattributes pattern for exactly this file (escape glob characters)."""
    pattern = re.sub(r"([\[\]*?\\ #!])", r"\\\1", file)
    return "/" + pattern


# ---------------------------------------------------------------------------
# Strategy 2: git log -G (pickaxe) — see pickaxe.py for the walking logic
# ---------------------------------------------------------------------------


_GIT_VERSION: tuple[int, ...] | None = None


def git_version() -> tuple[int, ...]:
    global _GIT_VERSION
    if _GIT_VERSION is None:
        try:
            out = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=30).stdout
            m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
            _GIT_VERSION = tuple(int(x) for x in m.groups() if x) if m else (0,)
        except (OSError, subprocess.TimeoutExpired):
            _GIT_VERSION = (0,)
    return _GIT_VERSION


def supports_remerge_diff() -> bool:
    """``--remerge-diff`` arrived in git 2.36 (April 2022)."""
    return git_version() >= (2, 36)


def merge_diff_args() -> list[str]:
    """
    How to diff a merge commit so that only *the merge's own edits* show up.

    Preferred: ``--remerge-diff`` — git redoes the merge automatically and diffs
    that against what was actually committed. Conflict resolutions, dropped
    hunks and "evil merge" edits appear; a clean landing shows nothing.

    Fallback (git < 2.36): diff against the first parent. That also shows the
    side branch's own changes landing, so callers must de-duplicate against the
    non-merge commits they already know.
    """
    return ["--remerge-diff"] if supports_remerge_diff() else ["--diff-merges=first-parent"]


def pickaxe_search(
    repo: Path, file: str, regex: str, *, merges: bool = False, extra_paths: list[str] | None = None
) -> list[Step]:
    """
    Commits whose diff of ``file`` has a +/- line matching ``regex``, newest first.

    * ``-U0`` means no context lines, so hunks contain only real changes.
    * Ordinary mode runs two queries: ``--follow`` from the current path to
      *discover* renames, then ``--full-history`` over every path the file has
      had. ``--follow`` alone is known to skip commits on side branches (it
      tracks one path at a time and still applies history simplification), and
      plain path limiting hides e.g. deletions that a merge later confirmed.
    * ``merges=True`` instead inspects *merge commits only* (see
      :func:`merge_diff_args`). ``--follow`` cannot be combined with that, so
      callers pass the file's historical paths in ``extra_paths``.
    """
    base = ["log", f"-G{regex}", "-p", "-U0", "-M", "--no-color", f"--format={FORMAT}"]
    if merges:
        paths = _unique([file, *(extra_paths or [])])
        return parse_log(run_git([*base, "--merges", *merge_diff_args(), "--", *paths], cwd=repo))

    followed = parse_log(run_git([*base, "--follow", "--", file], cwd=repo))
    discovered = [p for s in followed for p in (s.file_before, s.file_after) if p]
    paths = _unique([file, *(extra_paths or []), *discovered])
    full = parse_log(run_git([*base, "--full-history", "--", *paths], cwd=repo))

    by_sha: dict[str, Step] = {s.sha: s for s in full}
    by_sha.update({s.sha: s for s in followed})
    return sorted(by_sha.values(), key=lambda s: s.timestamp, reverse=True)


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def merge_commit_diff(repo: Path, sha: str, paths: list[str]) -> Step | None:
    """The merge's own edits to ``paths`` (see :func:`merge_diff_args`)."""
    args = ["log", "-1", *merge_diff_args(), "-p", "-U0", "--no-color", f"--format={FORMAT}", sha, "--", *paths]
    try:
        steps = parse_log(run_git(args, cwd=repo))
    except GitError:
        return None
    return steps[0] if steps else None


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    try:
        run_git(["merge-base", "--is-ancestor", older, newer], cwd=repo)
        return True
    except GitError:
        return False


# ---------------------------------------------------------------------------
# Merge topology helpers (used by merges.py)
# ---------------------------------------------------------------------------


def mainline(repo: Path) -> list[str]:
    """First-parent ancestry of HEAD, newest first."""
    return run_git(["rev-list", "--first-parent", "HEAD"], cwd=repo).split()


def merges_between(repo: Path, sha: str) -> list[str]:
    """Merge commits on any ancestry path from ``sha`` to HEAD, oldest first."""
    try:
        out = run_git(["rev-list", "--ancestry-path", "--merges", "--reverse", f"{sha}..HEAD"], cwd=repo)
    except GitError:
        return []
    return out.split()


def commit_details(repo: Path, sha: str) -> Step | None:
    """Header-only Step for a single commit (no diff)."""
    try:
        out = run_git(["log", "-1", f"--format={FORMAT}", "--no-color", sha], cwd=repo)
    except GitError:
        return None
    steps = parse_log(out)
    return steps[0] if steps else None


# ---------------------------------------------------------------------------
# Parser shared by every strategy
# ---------------------------------------------------------------------------


def parse_log(text: str) -> list[Step]:
    """
    Walk through git's output line by line::

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
            _, sha, short, author, email, date, ts, parents, subject, body = (parts + [""] * 10)[:10]
            current = Step(sha, short, author, email, date, int(ts or 0), subject, body, parents=parents.split())
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
            # Body lines follow the header (because %b spans multiple lines).
            # Bullet lists starting with "-" are safe: we check in_body first.
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
