# git-why 🪦

> `git blame` tells you *who*. `git why` tells you *why*.

Point it at a line and it reconstructs the **decision history**: where the line was
born, which bug it fixed, every meaningful change since — following the line
through renames and ignoring formatter noise.

```
$ git why src/payments/retry.py:8

  src/payments/retry.py:8
  │             time.sleep(0.3 * attempt)

  ● 2021-03-14  5bfece0  "add retry backoff"  (dima)
  │   line created here in src/utils.py
  │   Fixes #412: Stripe webhook double-charges under load because we
  │   retried instantly. Adding a linear backoff.
  └─ fixes #412
  ○ 2022-07-02  27e8996  style: run formatter   (whitespace / formatting only, skipped)
  ● 2023-11-19  ca95950  "reduce backoff 1.0 -> 0.3"  (alex)
  │   -             time.sleep(1.0  *  attempt)
  │   +             time.sleep(0.3 * attempt)
  │   p99 latency regressed after the retry change. 0.3 was tuned
  │   empirically against the staging dashboard. See PR #1180.
  └─ PR #1180

  Summary: Introduced on 2021-03-14 by dima ("add retry backoff") — linked to
  fixes #412; changed meaningfully 1 time since, most recently on 2023-11-19 by
  alex; 1 formatting/move commit ignored.
```

Compare with `git blame`, which would only say: *alex, "reduce backoff"*.

## Install

```bash
pip install git-why        # (coming soon)
# or from source:
pip install -e .
```

Git automatically picks up any `git-<name>` executable, so both work:

```bash
git-why path/to/file.py:42
git why  path/to/file.py:42
git why  path/to/file.py:40-55     # a range
git why  path/to/file.py:42 --all  # don't hide noise commits
```

## How it works

1. `git log -L <lines>:<file>` — git's little-known line-tracking log. It follows
   the lines backwards through history, even across renames.
2. Each commit's hunk is compared with whitespace stripped. Identical ⇒ **noise**
   (formatter runs, moves) and gets dimmed.
3. Commit messages are scanned for `Fixes #412`, `PR #1180`, `PAY-77` so the
   human reasoning is one hop closer.
4. Works 100 % offline. No AI required — the reasoning was already in your repo,
   it was just buried.

## Roadmap

- [ ] `--github` : fetch linked issue / PR titles and review comments
- [ ] `--function` : target a whole function by name instead of line numbers
- [ ] `--adr` : generate an Architecture Decision Record for a file from its history
- [ ] `--summarize` : optional LLM one-paragraph summary
- [ ] VS Code hover extension
- [ ] Smarter noise: detect mass renames (`sed`-style), import reordering

## Development

```bash
pip install -e . pytest
pytest
```

MIT License.
