# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `--profile NAME` selects the matching isolated omp profile for usage and cached readings;
  only that profile's authenticated providers appear. Provider headings include that
  provider's account email in white, for example `Codex (user@example.com)`.
- `--github USER` (config: `github.account`) selects a `gh` login for MY PRS and REVIEW
  REQUESTED without switching the active account.
- OpenRouter per-key spend, remaining credits, and key-limit display using the selected
  omp profile's credential.
- Nous Portal subscription and remaining credits when the selected omp profile has a
  `nous-portal` or `nous` credential.
- Pace icons on weekly and monthly usage bars: `>` to `>>>` when you use a quota faster than
  time passes, `<` to `<<<` when slower, `|` on pace. Turn them off with `usage.pace = false`.

### Fixed

- CI badge uses the latest Actions run of each workflow, then the latest check of each
  name, matching the PR merge box. A cancelled workflow's failed `Verify / gate` no
  longer keeps a later green run red, or a still-running rerun that has not posted gate yet.

## [0.1.0] - 2026-09-23

### Added

- Live pane of your open pull requests, grouped by repository, with gh-stack stacks and branch
  chains drawn as stacks.
- Status row per PR: CI, review decision, reviewer verdicts, bot reviews, unresolved threads,
  conflicts, behind-base, and merge-queue position.
- REVIEW REQUESTED section.
- Optional AI usage section from `omp usage`.
- TOML configuration, including repository and owner exclusions.

[Unreleased]: https://github.com/shayan-ys/devdash/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/shayan-ys/devdash/releases/tag/v0.1.0
