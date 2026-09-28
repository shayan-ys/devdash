#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["rich>=13"]
# ///
"""Dev-Dashboard (devdash): a narrow terminal dashboard for GitHub PRs, GitHub stacks, reviews, and OMP AI usage.

Sources (both are the tools' own JSON):
  - `gh api graphql`              your open PRs, grouped into native gh-stack
                                  stacks (or base->head chains for older ones),
                                  and PRs where your review is requested
  - `omp usage --json`   every AI provider's limits and reset times
                                  (optional; shown only when `omp` is installed)

Settings come from a TOML file (see --config); command-line flags win.
Keys while watching: r = refresh now, q = quit.
"""

import argparse
import calendar
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import time
import tomllib
import tty
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.request import Request, urlopen

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

__version__ = "0.1.0"
CONSOLE = Console()


def xdg(var, fallback, *parts):
    return os.path.join(os.environ.get(var) or os.path.expanduser(fallback), "devdash", *parts)


CONFIG_PATH = xdg("XDG_CONFIG_HOME", "~/.config", "config.toml")
LAST_GOOD = xdg("XDG_CACHE_HOME", "~/.cache", "last-usage.json")

# Palette: headings are bright and bold, body text is plain, metadata is dim.
H1 = "bold bright_cyan"
H2 = "bold bright_white"
STACK = "magenta"
NUM = "bold bright_blue"
META = "grey62"
OK, WARN, BAD, INFO = "green3", "yellow3", "red1", "cyan"
RULE = "#3d444d"
# Rows under a PR's first title row start this far in, so the left edge shows only PR numbers.
INDENT = " "


def dur(sec):
    sec = max(0, int(sec))
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d:
        return f"{d}d{h:02d}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m" if m else f"{sec}s"


def run(cmd, timeout=45, env=None):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if p.returncode:
        tail = (p.stderr or p.stdout).strip().splitlines()
        raise RuntimeError(tail[-1] if tail else f"{cmd[0]} failed")
    return p.stdout


def github_env(account):
    """Use a named gh login for this process only; never switch the active account."""
    if not account:
        return None
    p = subprocess.run(["gh", "auth", "token", "-u", account],
                       capture_output=True, text=True, timeout=10)
    token = p.stdout.strip() if p.returncode == 0 else ""
    if not token:
        tail = (p.stderr or "").strip().splitlines()
        raise RuntimeError(tail[-1] if tail else f"gh is not signed in as {account}")
    env = os.environ.copy()
    env["GH_TOKEN"] = env["GITHUB_TOKEN"] = token
    return env


def line(*parts):
    """One screen row; Rich truncates it with an ellipsis at the pane width."""
    t = Text(no_wrap=True, overflow="ellipsis")
    for p in parts:
        if isinstance(p, Text):
            t.append_text(p)
        elif isinstance(p, tuple):
            t.append(*p)
        elif p:
            t.append(p)
    return t


# ── config ────────────────────────────────────────────────────────────────────
DEFAULTS = {
    "interval": 60,
    "github": {"exclude": [], "review_requested": True, "account": ""},
    "usage": {"enabled": "auto", "refetch_after": 180, "pace": True, "order": [], "names": {}, "colors": {}},
}
REPO_RULE = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$")


def merge(base, over, where=""):
    """`over` on top of `base`, rejecting unknown keys and wrong types so a typo fails loudly."""
    out = dict(base)
    for key, val in over.items():
        name = f"{where}{key}"
        if key not in base:
            raise SystemExit(f"devdash: unknown config key '{name}'")
        want = base[key]
        if isinstance(want, dict) and want and isinstance(val, dict):
            out[key] = merge(want, val, f"{name}.")
        elif isinstance(want, dict) and not isinstance(val, dict):
            raise SystemExit(f"devdash: config key '{name}' must be a table")
        elif isinstance(want, bool) and not isinstance(val, bool):
            raise SystemExit(f"devdash: config key '{name}' must be true or false")
        elif type(want) is int and type(val) is not int:  # bool is an int subclass
            raise SystemExit(f"devdash: config key '{name}' must be a whole number")
        elif isinstance(want, list) and not isinstance(val, list):
            raise SystemExit(f"devdash: config key '{name}' must be a list")
        else:
            out[key] = val
    return out


def load_config(path, explicit):
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        if explicit:
            raise SystemExit(f"devdash: config file not found: {path}") from None
        raw = {}
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"devdash: {path}: {e}") from None
    cfg = merge(DEFAULTS, raw)
    if cfg["usage"]["enabled"] not in (True, False, "auto"):
        raise SystemExit("devdash: config key 'usage.enabled' must be true, false, or \"auto\"")
    return cfg


def check_rules(rules):
    for r in rules:
        if not isinstance(r, str) or not REPO_RULE.match(r):
            raise SystemExit(f"devdash: exclude entry {r!r} must be 'owner' or 'owner/repo'")
    return rules


def is_excluded(repo, rules):
    """`rules` holds 'owner/repo' entries and bare 'owner' entries (every repo of that user or org)."""
    repo = repo.lower()
    owner = repo.partition("/")[0]
    return any(r.lower() in (repo, owner) for r in rules)


# ── usage ─────────────────────────────────────────────────────────────────────
PROVIDER_ORDER = ["anthropic", "openai-codex", "google-antigravity", "cursor",
                  "openrouter", "nous-portal"]
PROVIDER_NAME = {"anthropic": "Anthropic", "openai-codex": "Codex",
                 "google-antigravity": "Antigravity", "cursor": "Cursor",
                 "openrouter": "OpenRouter", "nous-portal": "Nous Portal"}
WINDOW_SHORT = {"5h": "5h", "7d": "7d", "weekly": "wk", "monthly": "mo", "extra": "extra"}
EXTRA_USAGE = ("openrouter", "nous-portal")


def fetch_usage(invalidate, profile=None):
    prefix = ["--profile", profile] if profile is not None else []
    if invalidate:
        subprocess.run(["omp", *prefix, "usage", "invalidate"], capture_output=True, timeout=30)
    data = json.loads(run(["omp", *prefix, "usage", "--json"]))
    have = {r["provider"] for r in data.get("reports", [])}
    for extra in (fetch_openrouter, fetch_nous_portal):
        report = extra(profile)
        if report and report["provider"] not in have:
            data.setdefault("reports", []).append(report)
            have.add(report["provider"])
    return data


def omp_token(profile, provider):
    """Return a provider credential from omp without logging or storing it."""
    prefix = ["--profile", profile] if profile is not None else []
    token = subprocess.run(["omp", *prefix, "token", provider],
                           capture_output=True, text=True, timeout=10)
    value = token.stdout.strip() if token.returncode == 0 else ""
    return value or None


def bearer_json(url, token):
    request = Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def epoch_ms(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()[:-1] + "+00:00" if value.strip().endswith("Z") else value.strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def extra_report(provider, limits):
    if not limits:
        return None
    return {"provider": provider, "fetchedAt": int(time.time() * 1000), "limits": limits}


def usd_amount(used, cap=None):
    amount = {"used": used, "unit": "usd"}
    if cap is not None:
        amount["limit"] = cap
        amount["usedFraction"] = used / cap if cap > 0 else 1.0
    return amount


def fetch_openrouter(profile):
    """Use omp's selected profile credential without persisting or logging the key."""
    try:
        token = omp_token(profile, "openrouter")
        if not token:
            return None
        key = bearer_json("https://openrouter.ai/api/v1/key", token)["data"]
        try:
            credits = bearer_json("https://openrouter.ai/api/v1/credits", token).get("data") or {}
        except (OSError, ValueError, KeyError, TypeError, TimeoutError):
            credits = {}
        period = key.get("limit_reset")
        used = key.get({"daily": "usage_daily", "weekly": "usage_weekly",
                        "monthly": "usage_monthly"}.get(period, "usage"))
        cap = num(key.get("limit"))
        limits = []
        if used is not None:
            limits.append({"label": "key spend", "amount": usd_amount(used, cap)})
        total_credits, total_usage = num(credits.get("total_credits")), num(credits.get("total_usage"))
        if total_credits is not None and total_usage is not None:
            limits.append({"label": "credits left",
                           "amount": usd_amount(max(0.0, total_credits - total_usage))})
        return extra_report("openrouter", limits)
    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
        return None


def fetch_nous_portal(profile):
    """Read Portal credits from omp's nous-portal/nous credential. Never reads Hermes auth files."""
    try:
        token = omp_token(profile, "nous-portal") or omp_token(profile, "nous")
        if not token:
            return None
        payload = bearer_json("https://portal.nousresearch.com/api/oauth/account", token)
        if not isinstance(payload, dict) or payload.get("error"):
            return None
        sub = payload.get("subscription") if isinstance(payload.get("subscription"), dict) else {}
        access = payload.get("paid_service_access") if isinstance(payload.get("paid_service_access"), dict) else {}
        monthly, remaining = num(sub.get("monthly_credits")), num(sub.get("credits_remaining"))
        total = num(access.get("total_usable_credits"))
        purchased = num(access.get("purchased_credits_remaining"))
        limits = []
        if monthly is not None and monthly > 0 and remaining is not None:
            used = max(0.0, monthly - remaining)
            lim = {"label": "subscription", "amount": usd_amount(used, monthly)}
            reset = epoch_ms(sub.get("current_period_end"))
            if reset:
                lim["window"] = {"id": "monthly", "resetsAt": reset}
            limits.append(lim)
        left = total if total is not None else remaining
        if left is not None and not limits:
            limits.append({"label": "credits left", "amount": usd_amount(left)})
        elif total is not None and remaining is not None and total != remaining:
            limits.append({"label": "credits left", "amount": usd_amount(total)})
        if purchased is not None and purchased > 0:
            limits.append({"label": "top-up", "amount": usd_amount(purchased)})
        return extra_report("nous-portal", limits)
    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
        return None


def carry_over(old, new):
    """Anthropic's usage endpoint answers 429 often, and omp then drops the
    provider into accountsWithoutUsage (and caches that gap for every other
    omp process). Show the provider's last good read instead, marked stale."""
    if not old:
        return new
    have = {r["provider"] for r in new.get("reports", [])}
    missing = {a["provider"] for a in new.get("accountsWithoutUsage", [])} - have
    kept = [dict(r, stale=True) for r in old.get("reports", []) if r["provider"] in missing]
    if kept:
        new["reports"] = new.get("reports", []) + kept
        new["accountsWithoutUsage"] = [a for a in new["accountsWithoutUsage"] if a["provider"] not in missing]
    return new


def last_good_path(profile=None):
    """Return the cache path for a selected omp profile, or the legacy default."""
    if profile is None:
        profile = os.environ.get("OMP_PROFILE")
    if profile is None:
        return LAST_GOOD
    suffix = hashlib.sha256(profile.encode()).hexdigest()
    return LAST_GOOD.removesuffix(".json") + f"-{suffix}.json"


def load_last_good(profile=None):
    """The last good read survives restarts, so a launch during a 429 still shows Anthropic."""
    try:
        with open(last_good_path(profile)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_last_good(data, profile=None):
    path = last_good_path(profile)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"reports": data.get("reports", [])}, f)
    os.replace(tmp, path)


def oldest_read(data):
    """Seconds since the oldest live (non-carried) provider read, or None."""
    times = [r["fetchedAt"] / 1000 for r in (data or {}).get("reports", [])
             if r.get("fetchedAt") and not r.get("stale")]
    return time.time() - min(times) if times else None


def short_label(limit):
    """'Claude 7 Day (Fable)' -> '7d fable', 'Other Models' -> 'other mo'."""
    win = limit.get("window") or {}
    wid = win.get("id") or (limit.get("scope") or {}).get("windowId") or ""
    s = limit["label"]
    s = re.sub(r"^(Claude|Gemini CLI)\s+(?=\w)", "", s)
    s = re.sub(r"\((shared)\)|\b(Models?|requests|Usage)\b", "", s)
    s = re.sub(r"\s*&\s*", "+", s)
    s = re.sub(r"\b\d+\s*(Hour|hours)\b", "5h" if wid == "5h" else r"\g<0>", s)
    s = re.sub(r"\b\d+\s*(Day|days)\b", "7d" if wid == "7d" else r"\g<0>", s)
    s = " ".join(re.sub(r"[()]", "", s).lower().split())
    tag = WINDOW_SHORT.get(wid, wid)
    if tag and tag not in s.split():
        s = f"{s} {tag}".strip()
    return s or tag


# Heat scale shared by every bar: green while there is room, amber, orange, red at the cap.
HEAT = [(0.0, (63, 185, 80)), (0.6, (227, 179, 65)), (0.8, (240, 136, 62)), (1.0, (248, 81, 73))]
TRACK = "#21262d"
INK = "#0d1117"
BRAND = {"anthropic": "#d97757", "openai-codex": "#10a37f",
         "google-antigravity": "#4f8ff7", "cursor": "#b392f0"}
LABEL = "#8b949e"
RESET_TIME, RESET_SOON = "#79c0ff", "bold #56d4dd"


def heat(frac):
    """Hex colour at `frac` on the heat scale, linearly interpolated."""
    frac = max(0.0, min(1.0, frac))
    for (a, ca), (b, cb) in zip(HEAT, HEAT[1:], strict=False):
        if frac <= b:
            k = (frac - a) / (b - a)
            return "#%02x%02x%02x" % tuple(round(x + (y - x) * k) for x, y in zip(ca, cb, strict=True))
    return "#%02x%02x%02x" % HEAT[-1][1]


def meter(frac, width, label, exhausted=False, icon=None):
    """A bar with its value printed inside, like omp's context meter. Each
    filled cell takes the heat colour of its own position, so a fuller bar
    runs from green through amber to red. `icon` (a pace icon) sits one space
    right of the value, or left for a slow '<' icon; the value never moves."""
    frac = 1.0 if exhausted else max(0.0, min(1.0, frac))
    left = (width - len(label)) // 2
    cells = [" "] * width
    cells[left:left + len(label)] = label
    icon_at = range(0)
    if icon is not None:
        at = left - 1 - len(icon) if icon.plain.startswith("<") else left + len(label) + 1
        if at >= 0 and at + len(icon) <= width:
            cells[at:at + len(icon)] = icon.plain
            icon_at = range(at, at + len(icon))
    full = round(frac * width)
    tip = heat(frac)
    t = Text(no_wrap=True)
    for i, ch in enumerate(cells[:width]):
        if i < full:
            t.append(ch, style=f"bold {INK} on {heat(i / max(1, width - 1))}")
        elif i in icon_at:
            t.append(ch, style=f"{icon.style} on {TRACK}")
        else:
            t.append(ch, style=f"bold {tip} on {TRACK}")
    return t


DAY = 86400
WINDOW_SECONDS = {"5h": 5 * 3600, "daily": DAY, "7d": 7 * DAY, "weekly": 7 * DAY}


def window_start(win):
    """Epoch seconds when a limit's window began, or None. omp gives no
    duration for calendar-month windows, so those start one month before reset."""
    reset = win.get("resetsAt")
    if not reset:
        return None
    reset /= 1000
    if win.get("durationMs"):
        return reset - win["durationMs"] / 1000
    if win.get("id") == "monthly":
        r = datetime.fromtimestamp(reset, UTC)
        y, m = (r.year, r.month - 1) if r.month > 1 else (r.year - 1, 12)
        return r.replace(year=y, month=m, day=min(r.day, calendar.monthrange(y, m)[1])).timestamp()
    span = WINDOW_SECONDS.get(win.get("id"))
    return reset - span if span else None


def pace(lim, now):
    """Points of quota used ahead of (+) or behind (-) the share of time gone,
    for multi-day windows, else None. Hidden early in a window, where 1% after
    an hour would read as a wild pace."""
    win = lim.get("window") or {}
    used = (lim.get("amount") or {}).get("usedFraction")
    start = window_start(win)
    if used is None or start is None or lim.get("status") == "exhausted":
        return None
    span, gone = win["resetsAt"] / 1000 - start, now - start
    if span < 2 * DAY or gone < max(DAY / 2, span / 10) or gone >= span:
        return None
    return round((used - gone / span) * 100)


# Pace icon by distance from on-pace, in points: > fast, < slow, | on pace.
PACE_STEPS = [(30, 3), (15, 2), (5, 1)]
FAST_STYLE = {1: "yellow3", 2: "dark_orange", 3: "bold red1"}
SLOW_STYLE = {1: "#79c0ff", 2: "#79c0ff", 3: "bold #79c0ff"}


def pace_icon(points):
    """'|' within 5 points of pace, then one to three '>' (too fast) or '<' (too slow)."""
    n = next((k for step, k in PACE_STEPS if abs(points) >= step), 0)
    if not n:
        return Text("|", style=OK)
    return Text((">" if points > 0 else "<") * n, style=(FAST_STYLE if points > 0 else SLOW_STYLE)[n])


def usage_rows(report):
    """Visible limits: shared quotas appear once, uncapped zero counters are hidden."""
    rows, seen = [], set()
    for lim in report.get("limits", []):
        key = (lim["label"], (lim.get("window") or {}).get("resetsAt"))
        amt = lim.get("amount") or {}
        if key in seen or (amt.get("usedFraction") is None and not amt.get("used")
                           and report["provider"] not in EXTRA_USAGE):
            continue
        seen.add(key)
        rows.append(lim)
    return rows


def render_usage(st, width, now):
    data = st.usage
    out = [line(("USAGE", H1), ("  fetched " + dur(now - st.usage_at) + " ago", META) if st.usage_at else "")]
    if st.usage_err:
        out.append(line((f"⚠ {st.usage_err}", BAD)))
    if not data:
        return out
    reports = sorted(data.get("reports", []),
                     key=lambda r: (PROVIDER_ORDER.index(r["provider"])
                                    if r["provider"] in PROVIDER_ORDER else 99, r["provider"]))
    per = {}
    for r in reports:
        per[r["provider"]] = per.get(r["provider"], 0) + 1
    rows = [(r, usage_rows(r)) for r in reports]
    lab_w = max((len(short_label(lim)) for _, ls in rows for lim in ls), default=4)
    bar_w = max(8, width - lab_w - 8)  # label, gap, bar, gap, 6-col reset
    idx = {}
    for r, limits in rows:
        p = r["provider"]
        idx[p] = idx.get(p, 0) + 1
        name = PROVIDER_NAME.get(p, p) + (f" #{idx[p]}" if per[p] > 1 else "")
        fetched = r.get("fetchedAt")
        stale = r.get("stale") or (fetched and now - fetched / 1000 > 600)
        note = (f"  ⚠ last read {dur(now - fetched / 1000)} ago", WARN) if stale and fetched else ""
        brand = BRAND.get(p, "bright_white")
        if idx[p] == 1 and r is not rows[0][0]:
            out.append(Text())
        email = (r.get("metadata") or {}).get("email") if st.omp_profile else None
        who = (f" ({email})", "bright_white") if email else ""
        out.append(line(("● ", brand), (name, f"bold {brand}"), who, note))
        for lim in limits:
            amt = lim.get("amount") or {}
            frac = amt.get("usedFraction")
            reset = (lim.get("window") or {}).get("resetsAt")
            if reset:
                left = reset / 1000 - now
                rst = (dur(left).rjust(6), RESET_SOON if left < 3600 else RESET_TIME)
            else:
                rst = (" " * 6, LABEL)
            label = short_label(lim).ljust(lab_w)
            if frac is None:  # an uncapped counter
                value = Text((f"${amt['used']:.2f}" if amt.get("unit") == "usd"
                              else f"{int(amt['used'])} {amt.get('unit', '')}").center(bar_w),
                             style=f"{LABEL} on {TRACK}")
            else:
                if amt.get("unit") == "usd" and amt.get("limit"):
                    val = f"${amt['used']:.0f}/${amt['limit']:.0f}"
                else:
                    val = f"{frac * 100:.0f}%"
                expired = stale and reset and reset / 1000 < now  # the old number no longer applies
                delta = pace(lim, now) if st.show_pace and not expired else None
                icon = pace_icon(delta) if delta is not None else None
                value = meter(frac, bar_w, val, lim.get("status") == "exhausted", icon)
                if expired:
                    value = Text("reset since last read".center(bar_w)[:bar_w], style=f"{LABEL} on {TRACK}")
            out.append(line((label + " ", LABEL), value, " ", rst))
    missing = sorted({PROVIDER_NAME.get(a["provider"], a["provider"]) for a in data.get("accountsWithoutUsage", [])})
    if missing:
        out.append(line((f"no data: {', '.join(missing)}", WARN)))
    return out


# ── pull requests ─────────────────────────────────────────────────────────────
PR_FIELDS = """
fragment P on PullRequest {
  number title url isDraft headRefName baseRefName updatedAt mergeStateStatus
  mergeQueueEntry { state position }
  repository { nameWithOwner }
  author { login }
  reviewDecision
  {stack}
  reviewThreads(first: 100) { nodes { isResolved } }
  latestReviews(first: 20) { nodes { state author { __typename login } } }
  viewerLatestReview { state }
  commits(last: 1) { nodes { commit { statusCheckRollup { state
    contexts(first: 100) { nodes { __typename
      {check_run}
      ... on StatusContext { context state createdAt } } } } } } }
}
"""
# gh-stack's `stack` field is not in every GitHub schema (GitHub Enterprise Server, for one).
# Without it, stacks are rebuilt from base-branch -> head-branch chains instead.
STACK_FIELD = ("stack { number size baseRefName "
               "entries(first: 30) { nodes { position pullRequest { number state title url } } } }")
CHECK_RUN_CORE = "... on CheckRun { name status conclusion databaseId startedAt }"
CHECK_RUN_SUITE = (
    CHECK_RUN_CORE[:-2]
    + " checkSuite { workflowRun { databaseId createdAt workflow { name } } } }"
)
HAS_STACK = True
HAS_WORKFLOW_RUN = True


def fetch_prs(excluded, review_requested, account=None):
    global HAS_STACK, HAS_WORKFLOW_RUN
    # Search qualifiers keep excluded repos from eating the 50-result pages;
    # is_excluded below is the backstop for anything the search still returns.
    skip = " ".join(f"-repo:{r}" if "/" in r else f"-user:{r}" for r in excluded)
    review = (f'review: search(query: "is:pr is:open review-requested:@me archived:false {skip}", '
              "type: ISSUE, first: 50) { nodes { ...P } }") if review_requested else ""
    body = f"""
query {{
  mine: search(query: "is:pr is:open author:@me archived:false {skip}", type: ISSUE, first: 50) {{ nodes {{ ...P }} }}
  {review}
}}"""
    while True:
        query = (PR_FIELDS
                 .replace("{stack}", STACK_FIELD if HAS_STACK else "")
                 .replace("{check_run}", CHECK_RUN_SUITE if HAS_WORKFLOW_RUN else CHECK_RUN_CORE)
                 + body)
        try:
            data = json.loads(run(["gh", "api", "graphql", "-f", f"query={query}"],
                                  env=github_env(account)))
            break
        except RuntimeError as e:
            msg = str(e)
            if HAS_STACK and "Field 'stack' doesn't exist" in msg:
                HAS_STACK = False
                continue
            if HAS_WORKFLOW_RUN and ("Field 'checkSuite' doesn't exist" in msg
                                     or "Field 'workflowRun' doesn't exist" in msg):
                HAS_WORKFLOW_RUN = False
                continue
            raise
    if data.get("errors") and not data.get("data"):
        raise RuntimeError(data["errors"][0].get("message", "graphql error"))
    d = data["data"]

    def keep(nodes):
        return [n for n in nodes if n and not is_excluded(n["repository"]["nameWithOwner"], excluded)]

    return keep(d["mine"]["nodes"]), keep(d["review"]["nodes"]) if review_requested else []


def workflow_run(ctx):
    run = ((ctx.get("checkSuite") or {}).get("workflowRun") or {})
    name = (run.get("workflow") or {}).get("name") or ""
    return name, run.get("databaseId") or 0, run.get("createdAt") or ""


def latest_contexts(nodes):
    """Latest Actions run per workflow, then the newest check of each name.

    GitHub's statusCheckRollup keeps every check-run on the SHA. A cancelled
    workflow's `Verify / gate` FAILURE stays listed after a new run starts,
    even before that new run has posted gate. The merge box follows the
    latest workflow run; the rollup `state` does not.
    """
    latest_run = {}
    for i, ctx in enumerate(nodes):
        if ctx.get("__typename") != "CheckRun":
            continue
        name, rid, created = workflow_run(ctx)
        if not (name and rid):
            continue
        sort = (rid, created, i)
        if name not in latest_run or sort > latest_run[name]:
            latest_run[name] = sort
    latest = {}
    for i, ctx in enumerate(nodes):
        if ctx.get("__typename") == "CheckRun":
            wname, rid, created = workflow_run(ctx)
            if wname and rid:
                keep = latest_run[wname]
                if (rid, created) != (keep[0], keep[1]):
                    continue
            key = ("check", ctx["name"])
            sort = (ctx.get("databaseId") or 0, ctx.get("startedAt") or "", i)
        else:
            key = ("status", ctx.get("context"))
            sort = (0, ctx.get("createdAt") or "", i)
        if key not in latest or sort > latest[key][0]:
            latest[key] = (sort, ctx)
    return [item[1] for item in latest.values()]


def ci_badge(pr):
    """(Text badge, name of the first failed check or '')."""
    nodes = pr["commits"]["nodes"]
    roll = nodes[0]["commit"]["statusCheckRollup"] if nodes else None
    if not roll:
        return Text("○ci", style=META), ""
    failed, running = [], 0
    for ctx in latest_contexts(roll["contexts"]["nodes"]):
        if ctx["__typename"] == "CheckRun":
            if ctx["status"] != "COMPLETED":
                running += 1
            elif ctx["conclusion"] in ("FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"):
                failed.append(ctx["name"])
        elif ctx["state"] in ("FAILURE", "ERROR"):
            failed.append(ctx["context"])
        elif ctx["state"] in ("PENDING", "EXPECTED"):
            running += 1
    if failed:
        return Text(f"✗ci{len(failed)}", style=f"bold {BAD}"), failed[0]
    if running:
        return Text(f"●ci{running or ''}", style=WARN), ""
    return Text("✓ci", style=OK), ""


DECISION = {"APPROVED": ("approved", f"bold {OK}"),
            "CHANGES_REQUESTED": ("changes", f"bold {BAD}"),
            "REVIEW_REQUIRED": ("needs review", WARN)}
REVIEW_MARK = {"APPROVED": ("✓", OK), "CHANGES_REQUESTED": ("✗", BAD),
               "COMMENTED": ("✎", INFO), "DISMISSED": ("–", META)}
MERGE_FLAG = {"DIRTY": ("⚠conflict", f"bold {BAD}"), "BEHIND": ("↓behind", WARN)}
QUEUE_STYLE = {"UNMERGEABLE": f"bold {BAD}", "MERGEABLE": f"bold {OK}"}


def status_row(pr, me, lead=None):
    ci, failed = ci_badge(pr)
    t = line(lead or "", INDENT, ci)

    def add(text, style=""):
        t.append("  ")
        t.append(text, style=style)

    if pr["isDraft"]:
        add("draft", META)
    if pr["reviewDecision"] in DECISION:
        add(*DECISION[pr["reviewDecision"]])
    bots = 0
    for rv in pr["latestReviews"]["nodes"]:
        a = rv["author"] or {}
        if a.get("login") == me:
            continue
        if a.get("__typename") == "Bot":
            bots += 1
            continue
        mark, style = REVIEW_MARK.get(rv["state"], ("?", META))
        t.append(" ")
        t.append(mark, style=style)
        t.append(a.get("login", "?"), style=style)
    if bots:
        add(f"bot✎{bots}", META)
    threads = sum(1 for th in pr["reviewThreads"]["nodes"] if not th["isResolved"])
    if threads:
        add(f"⚑{threads}", f"bold {STACK}")
    queue = pr.get("mergeQueueEntry")
    if queue:
        add(f"⇢queued #{queue['position']}", QUEUE_STYLE.get(queue["state"], f"bold {INFO}"))
    elif pr["mergeStateStatus"] in MERGE_FLAG:
        add(*MERGE_FLAG[pr["mergeStateStatus"]])
    if failed:
        add(failed, BAD)
    return t


def title_rows(pr, width, gutter="", dim=False, note=""):
    """'#773 fix(mobile-app): draw…' wrapped to at most two rows; the
    conventional prefix is dimmed so the summary reads first."""
    t = Text()
    t.append(f"#{pr['number']} ", style=(META if dim else NUM) + f" link {pr['url']}")
    if note:
        t.append(note + " ", style=META)
    m = re.match(r"^(\w+(\([^)]*\))?!?:\s*)(.*)$", pr["title"])
    prefix, body = (m.group(1), m.group(3)) if m else ("", pr["title"])
    t.append(prefix, style=META)
    t.append(body, style=META if dim or pr.get("isDraft") else "bright_white")
    room = max(10, width - len(gutter))
    first = t.wrap(CONSOLE, room)[0]
    rest = t[len(first.plain):]
    rest = rest[len(rest.plain) - len(rest.plain.lstrip()):]
    if not rest.plain:
        return [line(gutter, first)]
    more = list(rest.wrap(CONSOLE, room - len(INDENT)))
    second = more[0]
    if len(more) > 1:
        second.truncate(room - len(INDENT) - 1)
        second.append("…", style=META)
    return [line(gutter, first), line(gutter, INDENT, second)]


def group_mine(prs):
    """{repo: [("stack", header, [pr_or_merged_layer, ...] top-first) | ("single", pr)]}."""
    by_repo = {}
    for pr in prs:
        by_repo.setdefault(pr["repository"]["nameWithOwner"], []).append(pr)
    out = {}
    for repo, items in sorted(by_repo.items()):
        groups, used, stacks = [], set(), {}
        for pr in items:
            if pr.get("stack"):
                stacks.setdefault(pr["stack"]["number"], pr["stack"])
        for num, st in stacks.items():
            mine = {p["number"]: p for p in items if p.get("stack") and p["stack"]["number"] == num}
            layers = []
            for e in sorted(st["entries"]["nodes"], key=lambda e: -e["position"]):
                n = e["pullRequest"]["number"]
                layers.append(mine.get(n) or e["pullRequest"])
                used.add(n)
            groups.append(("stack", f"stack #{num} → {st['baseRefName']}", layers))
        # Stacks made before GitHub tracked them: follow base branch -> head branch.
        rest = [p for p in items if p["number"] not in used]
        heads = {p["headRefName"]: p for p in rest}
        children = {}
        for p in rest:
            if p["baseRefName"] in heads:
                children.setdefault(p["baseRefName"], []).append(p)
        for p in sorted(rest, key=lambda p: p["number"]):
            if p["baseRefName"] in heads:
                continue
            chain, cur = [p], p
            while len(children.get(cur["headRefName"], [])) == 1:
                cur = children[cur["headRefName"]][0]
                chain.append(cur)
            if len(chain) > 1:
                groups.append(("stack", f"chain → {p['baseRefName']}", chain[::-1]))
            else:
                groups.append(("single", p))
        out[repo] = groups
    return out


def rule(width):
    return line(("─" * width, RULE))


def render_prs(st, width, now):
    out = [rule(width)] if st.show_usage else []
    out.append(line(("MY PRS ", H1), (str(len(st.mine)), H1),
                    ("  fetched " + dur(now - st.prs_at) + " ago", META) if st.prs_at else ""))
    if st.prs_err:
        out.append(line((f"⚠ {st.prs_err}", BAD)))
    for repo, groups in group_mine(st.mine).items():
        out.append(Text())
        owner, _, name = repo.partition("/")
        out.append(line((name, f"{H2} underline"), (f"  {owner}", META)))
        for gi, g in enumerate(groups):
            if gi:
                out.append(Text())  # a blank row between stacks and lone PRs
            if g[0] == "single":
                out += title_rows(g[1], width) + [status_row(g[1], st.me)]
                continue
            _, header, layers = g
            bar = Text("┃ ", style=STACK)
            out.append(line(bar, (header, f"bold {STACK}")))
            for pr in layers:
                if "commits" in pr:
                    out += title_rows(pr, width, bar) + [status_row(pr, st.me, line(bar))]
                else:  # merged, closed, or someone else's layer: one dim row
                    state = pr.get("state", "").lower()
                    note = "✓merged" if state == "merged" else state
                    out += title_rows(pr, width, bar, dim=True, note=note)[:1]
    if not st.show_review:
        return out
    out += [Text(), rule(width), line(("REVIEW REQUESTED ", H1), (str(len(st.review)), H1)), Text()]
    if not st.review:
        out.append(line(("nothing waiting on you", META)))
    for ri, pr in enumerate(sorted(st.review, key=lambda p: p["updatedAt"], reverse=True)):
        if ri:
            out.append(Text())
        updated = datetime.fromisoformat(pr["updatedAt"].replace("Z", "+00:00")).timestamp()
        lead = line((pr["repository"]["nameWithOwner"].split("/")[-1], META), " ",
                    (f"@{(pr['author'] or {}).get('login', '?')}", WARN), " ",
                    (dur(now - updated), META))
        if (pr.get("viewerLatestReview") or {}).get("state"):
            lead.append("  re-requested", style=STACK)
        out += title_rows(pr, width) + [lead, status_row(pr, st.me)]
    return out


# ── loop ──────────────────────────────────────────────────────────────────────
class State:
    usage = None
    usage_at = 0
    usage_err = None
    omp_profile = None
    gh_account = None
    cache_profile = None
    usage_inval_at = 0
    mine, review = [], []
    prs_at = 0
    prs_err = None
    me = ""
    busy = False
    interval = 60
    show_usage = True
    show_review = True
    show_pace = True
    excluded = []


class Dashboard:
    """Rebuilt on every Live refresh, so resizes and countdowns stay current."""

    def __init__(self, st):
        self.st = st

    def __rich_console__(self, console, options):
        now = time.time()
        width = options.max_width
        rows = render_usage(self.st, width, now) + [Text()] if self.st.show_usage else []
        rows += render_prs(self.st, width, now)
        foot = f"{time.strftime('%H:%M:%S')} · every {self.st.interval}s · r refresh · q quit"
        if self.st.busy:
            foot = "refreshing… · " + foot
        height = options.height or console.height
        if len(rows) > height - 2:
            hidden = len(rows) - (height - 3)
            rows = rows[: height - 3] + [line((f"… {hidden} more rows", META))]
        yield Group(*rows, Text(), line((foot, META)))


def refresh(st, pool, usage_every, force):
    st.busy = True
    now = time.time()
    fu = None
    if st.show_usage:
        # Other omp sessions keep omp's shared cache fresh, so force a refetch only
        # when the oldest live read is older than usage_every, and never twice in a
        # minute: every forced call is another hit on rate-limited usage endpoints.
        since = now - st.usage_inval_at
        age = oldest_read(st.usage)
        invalidate = (usage_every is not None and since >= 60
                      and (force or age is None or age >= usage_every))
        fu = pool.submit(fetch_usage, invalidate, st.omp_profile)
    fp = pool.submit(fetch_prs, st.excluded, st.show_review, st.gh_account)
    if fu:
        try:
            st.usage, st.usage_at, st.usage_err = carry_over(st.usage, fu.result()), time.time(), None
            if invalidate:
                st.usage_inval_at = now
            save_last_good(st.usage, st.cache_profile)
        except Exception as e:  # keep the last good data on screen
            st.usage_err = str(e)[:80]
    try:
        (st.mine, st.review), st.prs_at, st.prs_err = fp.result(), time.time(), None
    except Exception as e:
        st.prs_err = str(e)[:80]
    st.busy = False


def parse_args():
    ap = argparse.ArgumentParser(prog="devdash", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", metavar="PATH",
                    help=f"TOML settings file (default $DEVDASH_CONFIG, else {CONFIG_PATH})")
    ap.add_argument("--profile", metavar="NAME",
                    help="use this omp profile for usage and its saved cache")
    ap.add_argument("--github", metavar="USER",
                    help="GitHub username for MY PRS and REVIEW REQUESTED "
                         "(config: github.account; from `gh auth status`)")
    ap.add_argument("-n", "--interval", type=int, help="seconds between refreshes (config: interval, 60)")
    ap.add_argument("--exclude", action="append", default=[], metavar="OWNER[/REPO]",
                    help="hide a repository, or every repository of an owner; repeatable, "
                         "adds to github.exclude")
    ap.add_argument("--no-review", action="store_true", help="hide the REVIEW REQUESTED section")
    ap.add_argument("--no-usage", action="store_true", help="hide the USAGE section")
    # Some providers' usage endpoints rate-limit hard, and omp drops a provider
    # instead of keeping its last report, so forced refetches are rationed.
    ap.add_argument("--usage-every", type=int, metavar="SECONDS",
                    help="force a usage refetch when omp's cached reads are older than this "
                         "(config: usage.refetch_after, 180; 0 = never)")
    ap.add_argument("--cached", action="store_true",
                    help="never force a usage refetch; show whatever omp's shared cache holds")
    ap.add_argument("--once", action="store_true", help="print one frame and exit")
    ap.add_argument("-V", "--version", action="version", version=f"devdash {__version__}")
    return ap.parse_args()


def main():
    args = parse_args()
    path = args.config or os.environ.get("DEVDASH_CONFIG")
    cfg = load_config(os.path.expanduser(path or CONFIG_PATH), explicit=bool(path))
    gh, usage = cfg["github"], cfg["usage"]

    if not shutil.which("gh"):
        raise SystemExit("devdash: needs the GitHub CLI (https://cli.github.com), then `gh auth login`")
    st = State()
    st.interval = args.interval or cfg["interval"]
    st.excluded = check_rules(list(gh["exclude"]) + args.exclude)
    st.gh_account = args.github or gh["account"] or None
    st.show_review = gh["review_requested"] and not args.no_review
    st.show_usage = not args.no_usage and (usage["enabled"] is True
                                           or (usage["enabled"] == "auto" and shutil.which("omp") is not None))
    PROVIDER_ORDER[:] = list(dict.fromkeys(usage["order"] + PROVIDER_ORDER))
    PROVIDER_NAME.update(usage["names"])
    BRAND.update(usage["colors"])
    st.show_pace = usage["pace"]
    usage_every = args.usage_every if args.usage_every is not None else usage["refetch_after"]
    if args.cached or usage_every == 0:
        usage_every = None

    # Start from omp's cache so the first frame never waits on, or loses, a
    # refetch; the saved last good reads fill any provider omp dropped.
    st.usage_inval_at = time.time()
    if st.show_usage:
        st.omp_profile = args.profile
        st.cache_profile = (args.profile if args.profile is not None
                            else os.environ.get("OMP_PROFILE"))
        st.usage = load_last_good(st.cache_profile)
    try:
        st.me = run(["gh", "api", "user", "--jq", ".login"], env=github_env(st.gh_account)).strip()
    except RuntimeError as e:
        hint = "; pass --github USER from `gh auth status`" if st.gh_account else "; run `gh auth login`"
        who = f" as {st.gh_account}" if st.gh_account else ""
        raise SystemExit(f"devdash: gh is not signed in{who} ({e}){hint}") from None
    pool = ThreadPoolExecutor(2)

    if args.once or not sys.stdin.isatty():
        refresh(st, pool, usage_every, False)
        CONSOLE.print(Dashboard(st), height=10_000)
        return

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    force = False
    try:
        with Live(Dashboard(st), console=CONSOLE, screen=True, refresh_per_second=1):
            while True:
                refresh(st, pool, usage_every, force)
                force = False
                deadline = time.time() + st.interval
                while (left := deadline - time.time()) > 0:
                    if select.select([sys.stdin], [], [], left)[0]:
                        key = os.read(fd, 1).decode(errors="ignore").lower()
                        if key == "q":
                            return
                        if key == "r":
                            force = True
                            break
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    main()
