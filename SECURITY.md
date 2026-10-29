# Security

## What git-why does and does not do

- It runs `git` read-only commands (`log`, `show`, `rev-list`, `rev-parse`,
  `merge-base`, `diff --quiet`, `cat-file`) in the repository you point it at.
  It never writes to the repository, never pushes, and never modifies config.
  For `--function` it passes a temporary attributes file via
  `-c core.attributesFile`, which affects only that one command.
- With `--github` it performs **GET** requests to the GitHub REST API over
  HTTPS only. Nothing is ever posted.
- The token is read from `GITHUB_TOKEN` or `GH_TOKEN`, sent only as an
  `Authorization` header to the configured API host, and is never printed,
  logged (including with `--debug`), serialised into JSON, or cached on disk.
  There is no on-disk cache at all.
- Repository content is never sent anywhere. Only issue/PR numbers and commit
  SHAs appear in API URLs.

## Hardening tips

- Use a fine-grained token with read-only *Issues* and *Pull requests*
  permissions, scoped to the repositories you need.
- For GitHub Enterprise, set `GITHUB_API_URL`; the client refuses non-HTTPS
  API URLs.
- Untrusted repositories can contain adversarial commit messages. git-why
  treats them as text only; rich markup is escaped before rendering.

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security → Advisories →
Report a vulnerability) rather than a public issue. You should receive an
acknowledgement within a few days.
