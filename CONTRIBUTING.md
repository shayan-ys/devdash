# Contributing to Dev-Dashboard

Thank you for your help. Bug reports, ideas, documentation fixes, and code are all welcome.

## Before you start

- For a bug, [open an issue](https://github.com/shayan-ys/devdash/issues/new/choose) with the
  output of `devdash --version`, your terminal, and what you saw.
- For a new feature or a large change, open an issue first, so that we can agree on the approach
  before you write the code.

## Local setup

You need [uv](https://docs.astral.sh/uv/) and a signed-in [GitHub CLI](https://cli.github.com).

```sh
git clone https://github.com/shayan-ys/devdash
cd devdash
uv run pytest            # tests
uv run ruff check .      # lint
uv run devdash --once     # run against your own GitHub account
```

## Guidelines

- devdash is one file, `devdash.py`, and has one runtime dependency, `rich`. Keep it that way
  unless an issue agrees otherwise.
- devdash only reads. It must never change anything on GitHub or in a provider account.
- Add a test in `tests/` when you change behavior that a plausible bug could break, such as
  exclusion rules, config validation, or stack grouping.
- If you change what the pane shows, regenerate the README image with
  `uv run python scripts/screenshot.py`. It uses made-up data only; never commit a capture of real
  repositories.
- Write commit messages in the imperative mood ("Add …", "Fix …"), and add a line to the
  `Unreleased` section of [CHANGELOG.md](CHANGELOG.md) for a user-visible change.

## Code of Conduct

Everyone who takes part agrees to the [Code of Conduct](CODE_OF_CONDUCT.md).
