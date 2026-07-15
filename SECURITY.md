# Security Policy

## Supported versions

Readfine is developed by a single maintainer. Security fixes are applied to the
latest release on the `master` branch only. Please run a current version before
reporting.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through either channel:

- **Preferred:** GitHub's [Private Vulnerability Reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability):
  use the **Report a vulnerability** button under the repository's **Security** tab.
- **Email:** security@readfine.app

Please include:

- a description of the issue and its impact,
- steps to reproduce (a proof of concept if possible),
- affected version / commit.

## What to expect

This is a best-effort, single-maintainer project. I will acknowledge reports as
soon as I can, keep you updated on progress, and credit you in the release notes
once a fix ships (unless you prefer to stay anonymous). Please give me a
reasonable window to release a fix before any public disclosure.

## Scope

Readfine is self-hosted: each operator runs their own instance and is
responsible for its deployment (TLS, reverse proxy, secrets, OS patching). Most
relevant are issues in the application code, for example authentication and
session handling, multi-user data isolation, SSRF in the feed fetcher/scraper,
XSS in rendered feed or AI content, and the handling of stored secrets (API
keys, feed passwords).
