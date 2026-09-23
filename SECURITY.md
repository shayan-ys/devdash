# Security policy

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Do not open a public issue. Report the problem privately through
[GitHub's private vulnerability reporting](https://github.com/shayan-ys/devdash/security/advisories/new).
You will get an answer within 7 days.

## Scope

devdash runs `gh` and, when installed, `omp` on your machine and reads their output. It does not
store credentials. The only file it writes is a cache of usage figures in
`$XDG_CACHE_HOME/devdash/`, which contains no tokens.
