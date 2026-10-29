# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- `git why --help` now shows the description and an *Examples* section (the
  text passed to `typer.Typer(help=...)` is ignored for single-command apps),
  and the usage line reads `git-why` instead of `main`.
- CLI help tests no longer depend on the terminal: typer forces colour under
  `GITHUB_ACTIONS` / `FORCE_COLOR` and styles the dashes of each option
  separately, which made a substring assertion on raw output fail only in CI.
  Tests now strip ANSI escapes, pin width/TERM, and check the *Options* panel
  against the options the command actually declares.

### Changed

- Merge commits listed by `git log -L` that merely combined both sides'
  changes (no edits of their own under `--remerge-diff`) are now dimmed as
  *pass-through merges* instead of being reported as "changed by the merge
  itself"; the summary counts them separately.
- Commit headers show `(committed YYYY-MM-DD)` when the commit date differs
  from the author date (rebase, cherry-pick), since the timeline is ordered by
  commit time. JSON steps gain a `committed` field (schema version unchanged).
- CI checks out full history for the self-smoke test and no longer calls
  `git why --help` (git intercepts `--help` and looks for a man page); it also
  asserts the documented exit codes.
- Project URLs now point to `https://github.com/Dastan6752/git-why`.

## [0.1.0] - 2026-10-29

First public release.

### Added

- `git why FILE:LINE` / `FILE:START-END` — reconstruct the decision history of a
  line or range: origin, every meaningful change, formatter/move commits dimmed
  as noise, issue/PR/ticket references extracted from commit messages.
- **Pickaxe walk** — recursive `git log -G` search through every text version
  of a line. Recovers side-branch changes that `git blame` and `git log -L`
  drop through history simplification, and fixes `-L` *range drift* after large
  reformatting commits. Recovered commits are tagged `invisible to git blame`.
  Each search combines `--follow` (to discover renames) with a `--full-history`
  pass over every known path, because `--follow` alone skips side-branch
  commits after a rename (observed on psf/requests).
- `--function NAME` — git-native function tracking (`git log -L :regex:file`),
  with git's built-in language diff drivers enabled so indented methods work;
  ambiguous names are rejected with concrete line-range suggestions.
- `--text 'exact line'` — trace a line that no longer exists (even in a deleted
  file).
- `--merges` — inspect merge commits with `git --remerge-diff` (git ≥ 2.36;
  first-parent fallback otherwise) and report edits made *by the merge itself*
  (conflict resolutions, silently dropped lines). Landing merges are always
  annotated, which also surfaces `Merge pull request #N` references.
- `--github` — fetch linked issues and pull requests (title, state, excerpt,
  labels, linked issues, review and issue comments) plus GitHub's own
  commit→PR association. Standard library only, optional `GITHUB_TOKEN` /
  `GH_TOKEN`, GitHub Enterprise via `GITHUB_API_URL`, graceful handling of rate
  limits, auth errors, private repositories and network failures.
- Output formats: rich terminal (auto when attached to a TTY), `--format plain`
  for pipes, `--format json` / `--json` (schema version 1) with commit URLs,
  evidence strength per commit, confidence level with reasons, and warnings.
- Documented exit codes (0 ok, 1 usage, 2 git, 3 no history) and graceful
  handling of binary files, untracked files, uncommitted edits, shallow clones,
  empty repositories and out-of-range lines.
- Test suite (unit, temporary-repository integration, mocked GitHub API, CLI),
  CI on Linux/macOS for Python 3.10–3.13 with ruff, mypy, build and twine
  checks, and a Trusted-Publishing release workflow with a manual approval gate.

[Unreleased]: https://github.com/Dastan6752/git-why/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Dastan6752/git-why/releases/tag/v0.1.0
