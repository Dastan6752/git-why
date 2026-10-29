"""
core.py — the investigation pipeline. The CLI is a thin wrapper around ``investigate``.

    resolve target ─► git log -L ─► pickaxe walk ─► merge scan ─► noise ─► GitHub ─► Report
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import functions, history, noise
from .github import GitHubClient, api_base_for, parse_remote, token_from_env
from .history import GitError, NotARepositoryError, RepoInfo, Step
from .merges import annotate_landing_merges, fill_merge_hunks, order_newest_first, scan_merge_resolutions
from .pickaxe import PickaxeWalker, merge_timelines
from .report import Report, Target

MAX_PICKAXE_LINES = 5  # walking each line of a big range would be slow and noisy
MAX_GITHUB_THREADS = 12  # distinct issues/PRs fetched per run


class UserError(Exception):
    """An error the user can act on. ``exit_code`` follows the documented table."""

    def __init__(self, message: str, exit_code: int = 1, hint: str | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.hint = hint


EXIT_OK = 0
EXIT_USAGE = 1  # bad target, missing file, ambiguous function
EXIT_GIT = 2  # not a repository, untracked file, binary, git failure
EXIT_NO_HISTORY = 3  # valid target, but no commits touch it


@dataclass
class Options:
    function: str | None = None
    text: str | None = None
    pickaxe: bool = True
    merges: bool = False
    github: bool = False
    github_repo: str | None = None  # "owner/name" override
    classify_noise: bool = True
    debug: Callable[[str], None] = field(default=lambda _msg: None)
    github_client_factory: Callable[..., GitHubClient] = GitHubClient


# ---------------------------------------------------------------------------


def investigate(path: Path, start: int | None, end: int | None, opts: Options) -> Report:
    repo, rel = _resolve(path, opts)
    content = history.head_content(repo.root, rel)

    if opts.text is None:
        if content is None:
            raise UserError(
                f"{rel} is not tracked at HEAD (untracked, or only staged); commit it first",
                EXIT_GIT,
            )
        if history.is_binary(repo.root, rel):
            raise UserError(f"{rel} is a binary file; line history is only meaningful for text", EXIT_GIT)

    warnings: list[str] = []
    if content is not None and history.worktree_differs(repo.root, rel):
        warnings.append(f"{rel} has uncommitted changes; line numbers refer to HEAD")

    file_lines = content.splitlines() if content is not None else []

    # ---- 1. work out the target and run git's native tracking --------------
    if opts.text is not None:
        target = Target(rel, 0, 0, [opts.text], text=opts.text)
        steps: list[Step] = []
        walk_texts = [opts.text]
        trim = True
    elif opts.function is not None:
        target, steps, fallback_note = _function_target(repo, rel, content or "", file_lines, opts.function)
        if fallback_note:
            warnings.append(fallback_note)
        walk_texts = [target.lines[0]] if target.lines else []
        trim = False  # the hunks cover the whole function; don't trim to the header
    else:
        if start is None or end is None:
            raise UserError("expected FILE:LINE or FILE:START-END", EXIT_USAGE)
        if start < 1 or end > len(file_lines):
            raise UserError(
                f"{rel} has {len(file_lines)} lines at HEAD; requested {start}-{end}",
                EXIT_USAGE,
                hint="line numbers refer to the committed file (HEAD)",
            )
        target = Target(rel, start, end, file_lines[start - 1 : end])
        steps = history.line_history(repo.root, rel, start, end)
        walk_texts = target.lines if len(target.lines) <= MAX_PICKAXE_LINES else []
        trim = True

    # ---- 2. pickaxe walk: recover what -L cannot see -----------------------
    lineage: set[str] = {t.strip() for t in target.lines}
    lineage_complete = False
    pickaxe_used = False
    if opts.pickaxe and walk_texts:
        walker = PickaxeWalker(repo.root)
        for text in walk_texts:
            walker.walk(rel, text, content if opts.text is None else None)
        pickaxe_used = walker.searches > 0 or bool(walker.found)
        lineage |= walker.lineage
        lineage_complete = any(s.is_origin for s in walker.found) or (
            # -L already reached a genuine birth and the walk found nothing newer to add
            bool(steps) and steps[-1].is_origin and not walker.found
        )
        steps = merge_timelines(steps, walker.found, walker.lineage if trim else None)
        opts.debug(f"pickaxe: {walker.searches} searches, {len(walker.found)} commits")
    elif steps and steps[-1].is_origin:
        lineage_complete = True

    # ---- 3. merges ---------------------------------------------------------
    fill_merge_hunks(repo.root, rel, steps, lineage)
    annotate_landing_merges(repo.root, steps)
    if opts.merges:
        extra = scan_merge_resolutions(repo.root, rel, lineage, steps)
        if extra:
            steps = merge_timelines(steps, extra, None)
        opts.debug(f"merge scan: {len(extra)} merge-made changes")
    steps = order_newest_first(repo.root, steps)

    # ---- 4. shallow clones -------------------------------------------------
    for s in steps:
        if s.sha in repo.shallow_boundaries:
            s.shallow_boundary = True
    if repo.shallow:
        warnings.append("shallow clone: history before the clone depth is not available")

    # ---- 5. noise ----------------------------------------------------------
    if opts.classify_noise:
        steps = noise.classify_all(steps)

    report = Report(
        target=target,
        repo=repo,
        steps=steps,
        lineage=lineage,
        lineage_complete=lineage_complete,
        pickaxe_used=pickaxe_used,
        merges_scanned=opts.merges,
        warnings=warnings,
    )

    # ---- 6. GitHub ---------------------------------------------------------
    remote = parse_remote(repo.remote_url)
    if opts.github_repo:
        owner, name = opts.github_repo.split("/", 1)
        remote = ("github.com", owner, name)
    if remote:
        host, owner, name = remote
        report.web_base = f"https://{host}/{owner}/{name}"
    if opts.github:
        _attach_github(report, remote, opts)

    return report


# ---------------------------------------------------------------------------


def _resolve(path: Path, opts: Options) -> tuple[RepoInfo, str]:
    if path.exists() and path.is_dir():
        raise UserError(f"{path} is a directory; point at a file", EXIT_USAGE)
    probe = path.parent if path.parent != Path("") else Path(".")
    if not probe.exists():
        raise UserError(f"{path} does not exist", EXIT_USAGE)
    if not path.exists() and opts.text is None:
        raise UserError(f"{path} does not exist", EXIT_USAGE, hint="to trace a deleted line use --text 'exact line'")
    try:
        repo = history.repo_info(probe.resolve())
    except NotARepositoryError as exc:
        raise UserError(f"{path} is not inside a git repository", EXIT_GIT) from exc
    except GitError as exc:
        raise UserError(str(exc), EXIT_GIT) from exc
    try:
        rel = path.resolve().relative_to(repo.root).as_posix()
    except ValueError as exc:
        raise UserError(f"{path} is outside the repository at {repo.root}", EXIT_USAGE) from exc
    return repo, rel


def _function_target(
    repo: RepoInfo, rel: str, content: str, file_lines: list[str], name: str
) -> tuple[Target, list[Step], str | None]:
    matches = functions.find_functions(content, name)
    if not matches:
        raise UserError(
            f"no function named {name!r} found in {rel}",
            EXIT_USAGE,
            hint=f"check the spelling, or use a line range: git why {rel}:START-END",
        )
    if len(matches) > 1:
        options = []
        for m in matches:
            s, e = functions.function_extent(content, m.line)
            options.append(f"  line {m.line}: {m.text.strip()}   ->  git why {rel}:{s}-{e}")
        raise UserError(
            f"{name!r} is defined {len(matches)} times in {rel}; git would silently pick the first",
            EXIT_USAGE,
            hint="be explicit with a line range:\n" + "\n".join(options),
        )
    match = matches[0]
    start, end = functions.function_extent(content, match.line)
    target = Target(rel, start, end, file_lines[start - 1 : end], function=name)

    driver = functions.diff_driver_for(rel)
    note: str | None = None
    try:
        steps = history.function_history(repo.root, rel, name, driver)
    except GitError as exc:
        # e.g. the header is not a "funcname line" for git's driver -> use the computed range
        steps = history.line_history(repo.root, rel, start, end)
        note = f"git's function tracking could not match {name!r} ({_short(exc)}); tracked lines {start}-{end} instead"
    return target, steps, note


def _attach_github(report: Report, remote: tuple[str, str, str] | None, opts: Options) -> None:
    status = report.github
    status.enabled = True
    if remote is None:
        status.warnings.append(
            "origin is not a GitHub remote; pass --github-repo OWNER/NAME to fetch issue and PR evidence"
        )
        return
    host, owner, name = remote
    token = token_from_env()
    client = opts.github_client_factory(owner, name, token=token, api_base=api_base_for(host))
    report.github = client.status
    opts.debug(f"github: {owner}/{name} via {client.api_base}, token {'present' if token else 'absent'}")

    # Which threads to fetch: the origin first (it usually holds the "why"),
    # then the newest meaningful changes.
    ordered = [s for s in report.meaningful]
    if ordered:
        ordered = [ordered[-1], *ordered[:-1]]

    numbers: list[int] = []
    for step in ordered:
        refs = report.refs(step)
        if not any(r.kind == "pr" for r in refs):
            prs = client.pulls_for_commit(step.sha)
            if prs:
                report.commit_prs[step.sha] = prs
        for r in report.refs(step):
            if r.repo is not None and r.repo.lower() != f"{owner}/{name}".lower():
                continue  # cross-repo reference; out of scope
            if r.number is not None and r.number not in numbers:
                numbers.append(r.number)
        if len(numbers) >= MAX_GITHUB_THREADS:
            break

    for number in numbers[:MAX_GITHUB_THREADS]:
        thread = client.thread(number)
        if thread is not None:
            report.threads[number] = thread
        if client.status.offline or client.status.rate_limited:
            break


def _short(exc: Exception) -> str:
    return str(exc).splitlines()[0][:120] if str(exc) else type(exc).__name__
