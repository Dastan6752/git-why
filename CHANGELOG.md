# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `--ask QUESTION` (single `FILE:LINE` target): answers from the report's own
  evidence using a **local Ollama model**. Every factual sentence must cite a
  commit / PR / issue / comment ID from the evidence pack; sentences without a
  valid citation are removed *and listed*; missing evidence is reported as
  "Not in the evidence". No default model — `--model` / `GIT_WHY_MODEL`, or the
  only installed model; otherwise the output lists installed models and how to
  compare them. Without Ollama the normal report is printed with short setup
  steps. Loopback-only by default (`GIT_WHY_ALLOW_REMOTE_MODEL=1` to override).
  JSON gains an `"ask"` block; the report schema is unchanged.
- `--ask` sentences must also contain a **verbatim quote** from the cited
  source, verified mechanically; sentences whose quote is missing or not found
  in the source are removed and listed with the reason. Prompt rules added:
  a reason may only be stated if the quoted text gives it; concerns in review
  comments are not decisions; end with "Not in the evidence:" when the reason
  is not stated. (Found by a real run that cited a comment while reversing it.)
- `--ask` reason check: a sentence that presents something as the reason is
  kept only if its quote itself states a reason and contains no hedging or
  question; a reason claim is never kept alongside "Not in the evidence". Curly
  single-quote pairs are recognised as quotes. (Found by a real run that turned
  a reviewer's warning into the decision.)
- `--ask` fallback: when no factual sentence survives the checks, the answer
  shows the most relevant comment as an exact quote with its link, then
  "The evidence does not explain the final decision." Removed sentences are
  now counted in normal output and listed only with `--show-removed` or in
  JSON (`removed`, plus new `fallback`). Evidence items carry `quote`.
- The fallback quote is the single most relevant exact sentence of that
  comment (not the whole comment); the full text stays in JSON `fallback.full`.
- `--ask` distinguishes **code-explanation** questions ("What does this line
  do?") from **history** questions ("Why was this added?") and says which it
  detected. Code questions get the source at HEAD as citable evidence —
  `[S1]` the line, `[S2]` the enclosing block (≤ 40 lines) — ahead of the
  commits, with a prompt that forbids historical claims from code; history
  questions keep commits/PRs/comments first and gain `[S1]` last. New validator
  rule: a sentence cited only to source code is removed if it makes a
  historical claim. Backtick quotes and whole-line code quotes count as
  verbatim quotes. JSON `ask` gains `mode`; the report schema is unchanged.
  (Found by a real run where the only quotable evidence was a commit title.)

## [0.1.1] - 2026-10-30

### Fixed

- JSON output now safely escapes Unicode characters, preventing
  Windows console encoding errors when using the --json option.
- Function extent detection no longer mistakes a `{` inside an f-string or
  dict literal on the first body line for the start of a brace block (affected
  `--function` ranges and the new `--ask` code context).
- `git why --help` now shows the description and an *Examples* section (the
  text passed to `typer.Typer(help=...)` is ignored for single-command apps),
  and the usage line reads `git-why` instead of `main`.
- CLI help tests no longer depend on the terminal: typer forces colour under
  `GITHUB_ACTIONS` / `FORCE_COLOR` and styles the dashes of each option
  separately, which made a substring assertion on raw output fail only in CI.
  Tests now strip ANSI escapes, pin width/TERM, and check the *Options* panel
  against the options the command actually declares.

### Changed

- The distribution package is now named `git-why-trace`; the installed CLI command remains `git-why`.
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

[Unreleased]: https://github.com/Dastan6752/git-why/compare/v0.1.1...HEAD
[0.1.0]: https://github.com/Dastan6752/git-why/releases/tag/v0.1.0
[0.1.1]: https://github.com/Dastan6752/git-why/releases/tag/v0.1.1
