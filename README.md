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

## It sees what `git blame` can't

Tested on a real repo (`psf/requests`, line `DEFAULT_RETRIES = 0`):

```
$ git why src/requests/adapters.py:81

  ● 2012-12-15  88d41430  "base adapter"  (Kenneth Reitz)
  │   line created here in requests/adapters.py
  ● 2014-09-10  2bc7762c  "Update how we handle retries ..."  (Ian Cordasco)  ⚡ invisible to git blame
  │   - DEFAULT_RETRIES = 0
  │   + DEFAULT_RETRIES = object()
  ● 2014-10-24  2eb7e3c8  "Add last few changes and add a quick test"  (Ian Cordasco)  ⚡ invisible to git blame
  │   - DEFAULT_RETRIES = object()
  │   + DEFAULT_RETRIES = 0

  Summary: ...; 2 commits recovered that git blame / git log -L cannot see.
```

Both `git blame` and `git log -L` report only one of these three commits. The
other two happened on a side branch (PR #2216) that ended up with the same text
as mainline, so git's *history simplification* skipped the whole branch.

## How it works

1. **Range tracking** — `git log -L <lines>:<file>`, git's line-tracking log.
   Fast and follows renames, but has the merge blind spot above and a tendency
   to "drift" onto neighbouring lines after big reformatting commits.
2. **The pickaxe walk** — `git log -G"<exact line>" --follow` finds every commit
   that ever added or removed that exact text, with no simplification. In the
   commit that *added* it we look for the most similar *removed* line in the
   same hunk: that's the previous version. Recurse. Each hop goes one version
   further into the past, until the line's birth.
3. **Merge** — the two timelines are united by commit, and the lineage of texts
   learned in step 2 is used to trim `-L`'s drift down to just our line.
4. **Noise** — hunks are compared with whitespace stripped. Identical ⇒
   formatter run / move ⇒ dimmed, not deleted.
5. **References** — commit messages are scanned for `Fixes #412`, `PR #1180`,
   `PAY-77` so the human reasoning is one hop closer.

Works 100 % offline. No AI required — the reasoning was already in your repo,
it was just buried.

### Known limitations

- Changes made *inside a merge commit* (conflict resolution) are invisible to
  both strategies, since git doesn't diff merges by default.
- If the identical line appears several times in a file, the pickaxe can't tell
  which one is yours and stops; `-L` carries the rest.
- Very generic lines (`}`, `pass`, `return`) are only tracked with `-L`.

## Roadmap

- [ ] `--github` : fetch linked issue / PR titles and review comments
- [ ] `--function` : target a whole function by name instead of line numbers
- [ ] `--adr` : generate an Architecture Decision Record for a file from its history
- [ ] `--summarize` : optional LLM one-paragraph summary
- [ ] VS Code hover extension
- [ ] Smarter noise: detect mass renames (`sed`-style), import reordering
- [ ] `--merges` : also inspect merge-commit conflict resolutions
- [x] Recover side-branch history that `git blame` / `git log -L` miss (pickaxe walk)

## Development

```bash
pip install -e . pytest
pytest
```

MIT License.
