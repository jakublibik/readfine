---
name: changelog
description: Write or revise a CHANGELOG.md entry. Use with every commit that changes something a user or self-hoster would notice, and when checking the Unreleased section before a deploy or release.
---

# Changelog entries

Entries go under `## [Unreleased]` in `CHANGELOG.md`, in the Keep a Changelog
categories already used there: `### Upgrade notes` (admins only: env vars, settings to
set, migrations that take time), `### Added`, `### Changed`, `### Fixed`, `### Removed`,
`### Security`.

## Does it need one?

Yes for anything a reader or self-hoster would notice. No for internal refactors,
tests, docs, formatting and renames without user impact.

## How to write it

- Say what changed, what it means for the reader, and what they have to do (a setting
  to turn on, an env var to set). Name UI paths in bold, like **Settings → Relevance**.
- Roughly 100 words, 200 at most for a large feature. No design rationale, no defence
  of a method, no walk through every state; that belongs in `/help`, `features.yml` or
  a code comment.
- Within a category, order by impact: the change most readers will notice first.
- A follow-up fix to something still unreleased is folded into the original entry by
  **rewriting** that entry, not by appending a paragraph or adding a `Fixed` line.
- Write like a person: no em or en dashes (use commas, parentheses or a new sentence),
  no "so that" chains, no hedging, no mechanical triads.

## Before a deploy or release

Compare `git log origin/master..dev` with `[Unreleased]` and add whatever notable is
missing. The full rules are in `RELEASING.md` under "Keep entries short".
