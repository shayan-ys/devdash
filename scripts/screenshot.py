"""Render docs/screenshot.svg from made-up data, so the README image never shows real work.

    uv run python scripts/screenshot.py
"""

import time
from pathlib import Path

from rich.console import Console

import devdash

NOW = time.time()
HOUR = 3600


def limit(label, window, frac, reset_in):
    return {"label": label, "window": {"id": window, "resetsAt": (NOW + reset_in) * 1000},
            "amount": {"usedFraction": frac}}


USAGE = {"reports": [
    {"provider": "anthropic", "fetchedAt": NOW * 1000, "limits": [
        limit("Claude 5 Hour", "5h", 0.42, 2 * HOUR + 1500), limit("Claude 7 Day", "7d", 0.71, 3 * 24 * HOUR)]},
    {"provider": "openai-codex", "fetchedAt": NOW * 1000, "limits": [
        limit("5 Hour", "5h", 0.12, 4 * HOUR), limit("7 Day", "7d", 0.93, 40 * 60)]},
]}


def checks(*states):
    runs = [{"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}
            for name, status, conclusion in states]
    state = ("FAILURE" if any(c == "FAILURE" for _, _, c in states)
             else "PENDING" if any(s != "COMPLETED" for _, s, _ in states) else "SUCCESS")
    return {"nodes": [{"commit": {"statusCheckRollup": {"state": state, "contexts": {"nodes": runs}}}}]}


PASS = checks(("test", "COMPLETED", "SUCCESS"), ("lint", "COMPLETED", "SUCCESS"))
RUNNING = checks(("test", "IN_PROGRESS", None), ("lint", "COMPLETED", "SUCCESS"))
FAIL = checks(("test (3.13)", "COMPLETED", "FAILURE"), ("lint", "COMPLETED", "SUCCESS"))


def pr(number, title, repo, head, base, ci, author="you", draft=False, decision="REVIEW_REQUIRED",
       reviews=(), threads=0, stack=None, merge="CLEAN", queue=None, age=HOUR, rerequested=False):
    return {
        "number": number, "title": title, "url": f"https://github.com/{repo}/pull/{number}", "isDraft": draft,
        "headRefName": head, "baseRefName": base, "mergeStateStatus": merge, "mergeQueueEntry": queue,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - age)),
        "repository": {"nameWithOwner": repo}, "author": {"login": author}, "reviewDecision": decision,
        "stack": stack, "reviewThreads": {"nodes": [{"isResolved": False}] * threads},
        "latestReviews": {"nodes": [{"state": s, "author": {"__typename": t, "login": who}}
                                    for who, s, t in reviews]},
        "viewerLatestReview": {"state": "COMMENTED"} if rerequested else None, "commits": ci,
    }


STACK = {"number": 12, "baseRefName": "main", "entries": {"nodes": [
    {"position": 1, "pullRequest": {"number": 201, "state": "MERGED", "title": "refactor(api): split the router",
                                    "url": "https://github.com/acme/api/pull/201"}},
    {"position": 2, "pullRequest": {"number": 204, "state": "OPEN", "title": "feat(api): add rate limits",
                                    "url": "https://github.com/acme/api/pull/204"}},
    {"position": 3, "pullRequest": {"number": 207, "state": "OPEN", "title": "feat(api): expose limits in headers",
                                    "url": "https://github.com/acme/api/pull/207"}},
]}}

MINE = [
    pr(204, "feat(api): add rate limits", "acme/api", "limits", "router", PASS, decision="APPROVED",
       reviews=[("dana", "APPROVED", "User"), ("lint-bot", "COMMENTED", "Bot")], stack=STACK),
    pr(207, "feat(api): expose limits in headers", "acme/api", "headers", "limits", RUNNING,
       reviews=[("sam", "COMMENTED", "User")], threads=2, stack=STACK),
    pr(88, "fix(web): keep the sidebar open after a reload", "acme/web", "sidebar", "main", FAIL,
       decision="CHANGES_REQUESTED", reviews=[("lee", "CHANGES_REQUESTED", "User")], threads=1),
    pr(91, "docs: explain the deploy flow", "acme/web", "deploy-docs", "main", PASS, draft=True, merge="BEHIND"),
]
REVIEW = [
    pr(312, "feat(cli): add --json output", "acme/cli", "json", "main", PASS, author="dana", age=2 * HOUR,
       reviews=[("sam", "APPROVED", "User")]),
    pr(95, "chore(deps): bump rich to 14", "acme/web", "rich-14", "main", RUNNING, author="lee", age=26 * HOUR,
       rerequested=True),
]


def main():
    st = devdash.State()
    st.usage, st.usage_at = USAGE, NOW - 12
    st.mine, st.review, st.prs_at, st.me = MINE, REVIEW, NOW - 4, "you"
    # A fixed environment: rich renders 80 columns under TERM=dumb and prefers $COLUMNS to `width`.
    console = Console(record=True, width=54, force_terminal=True, color_system="truecolor",
                      _environ={"TERM": "xterm-256color"})
    devdash.CONSOLE = console  # title wrapping measures against the module console
    console.print(devdash.Dashboard(st), height=10_000)
    out = Path(__file__).resolve().parent.parent / "docs" / "screenshot.svg"
    out.parent.mkdir(exist_ok=True)
    console.save_svg(str(out), title="Dev-Dashboard")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
