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
  - [[integrations]] commands     your own sections: any command whose output
                                  devdash shows (see the README)

Settings come from a TOML file (see --config); command-line flags win.
Keys while watching: configurable built-ins (r = refresh, q = quit, tab = focus) and focused integration bindings.
"""

import argparse
import calendar
import contextlib
import hashlib
import json
import math
import os
import queue
import re
import select
import selectors
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tomllib
import tempfile
import tty
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.request import Request, urlopen

from rich.console import Console, Group
from rich.live import Live
from rich.style import Style
from rich.text import Span, Text

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
    "keys": {"refresh": "r", "quit": "q", "focus": "tab", "scroll_up": "up", "scroll_down": "down"},
    "integrations": [],
}
REPO_RULE = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$")
SPECIAL_KEYS = {"up", "down", "left", "right", "enter", "tab"}


def valid_key_name(key):
    return (isinstance(key, str) and
            (key in SPECIAL_KEYS or (len(key) == 1 and "!" <= key <= "~")))


def check_builtin_keys(keys):
    enabled = {}
    for name in DEFAULTS["keys"]:
        key = keys[name]
        if not isinstance(key, str):
            raise SystemExit(f"devdash: config key 'keys.{name}' must be a key name or an empty string")
        if key and not valid_key_name(key):
            raise SystemExit(f"devdash: config key 'keys.{name}' must be a printable key name or an empty string")
        if key:
            if key in enabled:
                raise SystemExit(f"devdash: built-in keys '{enabled[key]}' and '{name}' both use '{key}'")
            enabled[key] = name
    return enabled


def check_key_name(key, where):
    if not valid_key_name(key):
        raise SystemExit(f"devdash: {where} key {key!r} must be one printable ASCII character or "
                         "'up', 'down', 'left', 'right', 'enter', or 'tab'")


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
    builtins = check_builtin_keys(cfg["keys"])
    cfg["integrations"] = check_integrations(cfg["integrations"], builtins)
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


def section_status(fetched_at, fetching, next_fetch_at, now, monotonic_now, watching=False):
    if fetching:
        return ("  fetching…", META)
    parts = []
    if fetched_at:
        parts.append(f"fetched {dur(now - fetched_at)} ago")
    if watching:
        parts.append("watching")
    elif next_fetch_at is not None:
        seconds = max(0, math.ceil(next_fetch_at - monotonic_now))
        parts.append(f"next fetch {dur(seconds)}")
    return ("  " + " · ".join(parts), META) if parts else ""


def render_usage(st, width, now, monotonic_now=None):
    if monotonic_now is None:
        monotonic_now = time.monotonic()
    data = st.usage
    status = section_status(st.usage_at, st.usage_fetching, st.global_next_fetch, now, monotonic_now)
    out = [line(("USAGE", H1), status)]
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


def render_mine(st, width, now, monotonic_now=None):
    if monotonic_now is None:
        monotonic_now = time.monotonic()
    status = section_status(st.prs_at, st.prs_fetching, st.global_next_fetch, now, monotonic_now)
    out = [line(("MY PRS ", H1), (str(len(st.mine)), H1), status)]
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
    return out


def render_review(st, width, now, monotonic_now=None):
    if monotonic_now is None:
        monotonic_now = time.monotonic()
    status = section_status(st.prs_at, st.prs_fetching, st.global_next_fetch, now, monotonic_now)
    out = [line(("REVIEW REQUESTED ", H1), (str(len(st.review)), H1), status), Text()]
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


# ── custom integrations ───────────────────────────────────────────────────────
# An integration is any command that prints its section body to stdout. devdash
# runs it without a shell, on its own thread and schedule, with no stdin and a
# timeout, and shows its last good output; colours and hyperlinks are kept, and
# every other escape sequence is dropped.
POSITIONS = ("top", "after-usage", "after-my-prs", "bottom")
INTEGRATION = {"name": "", "title": "", "command": [], "position": "bottom", "enabled": True,
               "interval": 0, "timeout": 10, "max_rows": 20, "env": {}, "keys": {}, "watch": []}
NAME_RULE = re.compile(r"[A-Za-z0-9_.-]+")
MAX_OUTPUT = 64 * 1024  # bytes of standard output devdash keeps; a command that prints more is stopped
ERR_TAIL = 4 * 1024     # bytes of standard error devdash keeps, the newest ones, for the error line
MAX_SECONDS = DAY       # upper bound for interval and timeout; threading and select reject huge waits
# C0 controls except tab and newline, DEL, and C1 controls: what is left of escape sequences
# that Rich does not decode. Each one becomes U+FFFD so styled spans keep their offsets.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def check_integrations(items, builtins=None):
    """Validate every integration, including disabled ones, and fill in its defaults."""
    if builtins is None:
        builtins = check_builtin_keys(DEFAULTS["keys"])
    specs, names = [], set()
    for i, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise SystemExit(f"devdash: integrations[{i}] must be a table")
        spec = merge(INTEGRATION, raw, f"integrations[{i}].")
        name = spec["name"]
        if not isinstance(name, str) or not NAME_RULE.fullmatch(name):
            raise SystemExit(f"devdash: integrations[{i}].name is required: letters, digits, '.', '_' or '-'")
        if name in names:
            raise SystemExit(f"devdash: integration name '{name}' is used twice")
        names.add(name)
        where = f"devdash: integration '{name}':"
        cmd = spec["command"]
        if not cmd or not all(isinstance(a, str) and a for a in cmd):
            raise SystemExit(f"{where} command must be a non-empty list of strings")
        if not isinstance(spec["title"], str):
            raise SystemExit(f"{where} title must be a string")
        if spec["position"] not in POSITIONS:
            raise SystemExit(f"{where} position must be one of {', '.join(POSITIONS)}")
        if not 0 <= spec["interval"] <= MAX_SECONDS:
            raise SystemExit(f"{where} interval must be 0 (the global interval) to {MAX_SECONDS}")
        if not isinstance(spec["watch"], list) or not all(isinstance(path, str) for path in spec["watch"]):
            raise SystemExit(f"{where} watch must be a list of paths (strings)")
        spec["watch"] = [os.path.expandvars(os.path.expanduser(path)) for path in spec["watch"]]
        if not 1 <= spec["timeout"] <= MAX_SECONDS:
            raise SystemExit(f"{where} timeout must be 1 to {MAX_SECONDS}")
        if spec["max_rows"] < 1:
            raise SystemExit(f"{where} max_rows must be 1 or more")
        if not all(isinstance(v, str) for v in spec["env"].values()):
            raise SystemExit(f"{where} env values must be strings")
        for key, action in spec["keys"].items():
            check_key_name(key, where)
            if not isinstance(action, str) or not NAME_RULE.fullmatch(action):
                raise SystemExit(f"{where} action for key {key!r} must use letters, digits, '.', '_' or '-'")
            if key in builtins:
                builtin = builtins[key]
                raise SystemExit(f"{where} key '{key}' conflicts with built-in '{builtin}'; "
                                 f"remap it in [keys] (for example, keys.{builtin} = \"x\")")
        spec["has_keys"] = "keys" in raw
        specs.append(spec)
    return specs


def output_rows(text, max_rows):
    """A command's ANSI output as screen rows, trailing blank rows dropped, at most `max_rows`.

    Only SGR styles and OSC 8 links survive: Rich decodes those, the leftover control
    characters are replaced, and a link whose target holds a control character is dropped."""
    body = Text.from_ansi(text.replace("\r\n", "\n"))
    body.plain = CONTROL.sub("\ufffd", body.plain)
    body.spans = [Span(s.start, s.end, s.style.update_link(None))
                  if isinstance(s.style, Style) and s.style.link and CONTROL.search(s.style.link) else s
                  for s in body.spans]
    body.expand_tabs()
    rows = [line(r) for r in body.split("\n")]
    while rows and not rows[-1].plain.strip():
        rows.pop()
    if len(rows) > max_rows:
        hidden = len(rows) - max_rows + 1
        rows = rows[:max_rows - 1] + [line((f"… {hidden} more rows", META))]
    return rows


def plain_line(text):
    """`text` as one row of plain characters, for messages built from a command's stderr."""
    return CONTROL.sub("", Text.from_ansi(text).plain.replace("\n", " "))


def kill_group(p):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(p.pid, signal.SIGKILL)
    p.wait()


def capture(p, deadline):
    """(stdout, stderr tail, stopped for size) of `p`, holding at most MAX_OUTPUT + ERR_TAIL bytes.

    Reads both pipes as data arrives, so a command that floods either one cannot grow
    devdash's memory. Stops the process group once stdout passes MAX_OUTPUT, or raises
    TimeoutExpired after stopping it at `deadline`.
    """
    out, err = bytearray(), bytearray()
    full = False
    with selectors.DefaultSelector() as sel:
        sel.register(p.stdout, selectors.EVENT_READ, out)
        sel.register(p.stderr, selectors.EVENT_READ, err)
        while sel.get_map() and not full:
            left = deadline - time.monotonic()
            if left <= 0:
                kill_group(p)
                raise subprocess.TimeoutExpired(p.args, 0)
            for key, _ in sel.select(left):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                elif key.data is err:
                    err += chunk
                    del err[:-ERR_TAIL]
                else:
                    out += chunk
                    full = len(out) > MAX_OUTPUT
    if full:
        kill_group(p)
        return out[:MAX_OUTPUT], err, True
    try:  # both pipes are closed, but the command may still be running
        p.wait(max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        kill_group(p)
        raise
    return out, err, False


class Integration:
    """One configured section. Its own thread publishes `result`; the render thread reads it."""

    def __init__(self, spec, interval):
        self.name = spec["name"]
        self.title = spec["title"] or spec["name"].upper()
        self.command = [os.path.expanduser(spec["command"][0]), *spec["command"][1:]]
        self.position = spec["position"]
        self.watch_paths = tuple(spec["watch"])
        self.watching = bool(self.watch_paths)
        # Watching replaces interval polling unless the integration sets its own interval.
        if spec["interval"] or not self.watching:
            self.interval = min(spec["interval"] or interval, MAX_SECONDS)  # the global interval is unbounded
        else:
            self.interval = 0
        self.timeout = spec["timeout"]
        self.max_rows = spec["max_rows"]
        self.env = spec["env"]
        self.keys = spec["keys"]
        self.has_keys = spec.get("has_keys", bool(self.keys))
        self.state_file = None
        self.result = ([], 0, None)  # (rows, fetched at, error)
        self.next_run_at = None
        self.wake = threading.Event()
        # stop(), fetch(), and the action queue share this lock, so a command started
        # while devdash quits is either never started or killed.
        self.lock = threading.Lock()
        self.proc = None
        self.running = False
        self.stopped = False
        self.actions = deque()
        self.refresh_requested = True

    def watch_signature(self):
        """Stat every path under the watch roots. Roots are followed; symlinks inside them are not."""
        signature = []
        for root in self.watch_paths:
            try:
                info = os.stat(root)
            except OSError:
                continue  # a missing root is watched for its creation
            signature.append((root, info.st_mtime_ns, info.st_size))
            if not os.path.isdir(root):
                continue
            pending = [root]
            while pending:
                directory = pending.pop()
                try:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            try:
                                item = entry.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            signature.append((entry.path, item.st_mtime_ns, item.st_size))
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(entry.path)
                except OSError:
                    continue
        return tuple(sorted(signature))

    def watch_loop(self):
        """Run again after watched paths change: once writes go quiet for 0.2 s, or 2 s into a
        continuous burst. Passes wait at least four times their own cost, so a big tree cannot hog a core."""
        started = time.monotonic()
        previous = self.watch_signature()
        cost = time.monotonic() - started
        changed_at = first_change = None
        while True:
            time.sleep(max(0.25, 4 * cost))
            with self.lock:
                if self.stopped:
                    return
            started = time.monotonic()
            current = self.watch_signature()
            now = time.monotonic()
            cost = now - started
            if current != previous:
                previous = current
                changed_at = now
                first_change = first_change or now
            if changed_at is not None and (now - changed_at >= 0.2 or now - first_change >= 2.0):
                self.request_refresh(after_current=True)
                changed_at = first_change = None

    def fetch(self, width, action=None):
        # COLUMNS and LINES tell the command how much room its section has.
        env = {**os.environ, **self.env, "COLUMNS": str(width), "LINES": str(self.max_rows)}
        env.pop("DEVDASH_ACTION", None)
        env.pop("DEVDASH_STATE_FILE", None)
        if action is not None:
            env["DEVDASH_ACTION"] = action
        if self.state_file is not None:
            env["DEVDASH_STATE_FILE"] = self.state_file
        with self.lock:
            if self.stopped:
                raise RuntimeError("stopped")
            try:
                # A new session puts the command and its children in one process group,
                # so a timeout kills them all; DEVNULL keeps it off devdash's keyboard.
                p = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env, start_new_session=True)
            except OSError as e:
                raise RuntimeError(f"cannot run {self.command[0]}: {e.strerror}") from None
            self.proc = p
        try:
            out, err, full = capture(p, time.monotonic() + self.timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"timed out after {self.timeout}s") from None
        except BaseException:
            if p.returncode is None:  # never leave a command running behind an unexpected error
                kill_group(p)
            raise
        finally:
            with self.lock:
                self.proc = None
            p.stdout.close()
            p.stderr.close()
        if p.returncode and not full:  # a command stopped for printing too much still shows its output
            tail = err.decode(errors="replace").strip().splitlines()
            raise RuntimeError(f"exit {p.returncode}" + (f": {tail[-1]}" if tail else ""))
        return output_rows(out.decode(errors="replace"), self.max_rows)

    def poll(self, width, action=None):
        """Run the command once. A failure keeps the last good rows on screen under the error.

        `result` is (rows, fetched at, error), replaced as one tuple so a frame never mixes two runs."""
        try:
            self.result = (self.fetch(width, action), time.time(), None)
        except Exception as e:  # one broken integration must never stop the dashboard
            rows, at, _ = self.result
            self.result = (rows, at, plain_line(str(e))[:80])

    def queue_action(self, action):
        with self.lock:
            if self.stopped or len(self.actions) >= 8:
                return False
            self.actions.append(action)
            self.wake.set()
            return True

    def request_refresh(self, after_current=False):
        with self.lock:
            if not self.stopped and (not self.running or after_current):
                self.refresh_requested = True
            self.wake.set()

    def loop(self, st):
        if self.watching:
            threading.Thread(target=self.watch_loop, daemon=True, name=f"integration-watch-{self.name}").start()
        next_run = time.monotonic()
        while True:
            self.wake.clear()
            now = time.monotonic()
            with self.lock:
                if self.stopped:
                    return
                action = self.actions.popleft() if self.actions else None
                refresh_requested = self.refresh_requested
                self.refresh_requested = False
                run_now = action is not None or refresh_requested or (self.interval > 0 and now >= next_run)
                if run_now:
                    self.running = True
            if run_now:
                try:
                    self.poll(st.width, action)
                finally:
                    next_run = time.monotonic() + self.interval
                    with self.lock:
                        self.running = False
                        self.next_run_at = next_run if self.interval > 0 else None
                    if st.live is not None:
                        st.live.refresh()
                continue
            self.wake.wait(max(0, next_run - time.monotonic()) if self.interval > 0 else None)

    def stop(self):
        """Stop polling and kill a running command with everything it started.

        The command runs in its own session, so it would outlive devdash otherwise."""
        with self.lock:
            self.stopped = True
            p = self.proc
        self.wake.set()
        if p is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)


@contextlib.contextmanager
def integration_state_files(st):
    """Give watch-mode integrations with a keys table one private, empty state file."""
    keyed = [ig for ig in st.integrations if ig.has_keys]
    if not keyed:
        yield
        return
    with tempfile.TemporaryDirectory(prefix="devdash-") as directory:
        os.chmod(directory, 0o700)
        try:
            for index, ig in enumerate(keyed):
                path = os.path.join(directory, f"{index}.state")
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.fchmod(fd, 0o600)
                os.close(fd)
                ig.state_file = path
            yield
        finally:
            for ig in keyed:
                ig.state_file = None


KEY_ESCAPE_TIMEOUT = 0.05


def decode_keys(data, pending=b"", flush=False):
    """Decode bindable key names while preserving an incomplete trailing escape sequence."""
    buf = pending + data
    keys, index = [], 0
    while index < len(buf):
        byte = buf[index]
        if byte == 0x1B:
            if index + 1 == len(buf):
                if not flush:
                    return keys, buf[index:]
                index += 1
                continue
            intro = buf[index + 1]
            if intro in (ord("["), ord("O")):
                final = index + 2
                while final < len(buf) and not 0x40 <= buf[final] <= 0x7E:
                    final += 1
                if final == len(buf):
                    if not flush:
                        return keys, buf[index:]
                    break
                if final == index + 2 and buf[final] in (ord("A"), ord("B"), ord("C"), ord("D")):
                    keys.append({"A": "up", "B": "down", "C": "right", "D": "left"}[chr(buf[final])])
                index = final + 1
                continue
            if intro in (ord("]"), ord("P"), ord("X"), ord("^"), ord("_")):
                end, terminated = index + 2, False
                while end < len(buf):
                    if intro == ord("]") and buf[end] == 0x07:
                        end += 1
                        terminated = True
                        break
                    if buf[end] == 0x1B:
                        if end + 1 == len(buf):
                            break
                        if buf[end + 1] == ord("\\"):
                            end += 2
                            terminated = True
                            break
                    end += 1
                if not terminated:
                    if not flush:
                        return keys, buf[index:]
                    break
                index = end
                continue
            if intro == 0x1B:
                index += 1
                continue
            # discard any other escape whole: ESC, intermediates 0x20-0x2F, then one final byte
            final = index + 1
            while final < len(buf) and 0x20 <= buf[final] <= 0x2F:
                final += 1
            if final == len(buf):
                if not flush:
                    return keys, buf[index:]
                break
            index = final + 1
            continue
        if byte in (0x0A, 0x0D):
            keys.append("enter")
        elif byte == 0x09:
            keys.append("tab")
        elif 0x21 <= byte <= 0x7E:
            keys.append(chr(byte))
        index += 1
    return keys, b""


def screen_integrations(st):
    return [ig for position in POSITIONS for ig in st.integrations if ig.position == position]


def focusable_integrations(st):
    return [ig for ig in screen_integrations(st) if ig.keys]


def focus_next(st):
    integrations = focusable_integrations(st)
    if len(integrations) < 2:
        return False
    current = next((i for i, ig in enumerate(integrations) if ig is st.focus), -1)
    st.focus = integrations[(current + 1) % len(integrations)]
    return True


def dispatch_key(st, key):
    """Apply one key press; built-ins take precedence over the focused integration."""
    result = "ignored"
    if key == st.keys["quit"] and st.keys["quit"]:
        result = "quit"
    elif key == st.keys["refresh"] and st.keys["refresh"]:
        for ig in st.integrations:
            ig.request_refresh()
        result = "refresh"
    elif key == st.keys["focus"] and st.keys["focus"]:
        focus_next(st)
    elif key == st.keys["scroll_up"] and st.keys["scroll_up"]:
        st.scroll = max(0, st.scroll - 1)
    elif key == st.keys["scroll_down"] and st.keys["scroll_down"]:
        st.scroll += 1  # Dashboard clamps it to the content at the next render
    elif st.focus is not None and key in st.focus.keys:
        st.focus.queue_action(st.focus.keys[key])
    if st.live is not None:
        st.live.refresh()
    return result


def render_integration(ig, now, focused=False, monotonic_now=None):
    if monotonic_now is None:
        monotonic_now = time.monotonic()
    rows, at, err = ig.result
    status = section_status(at, ig.running, ig.next_run_at, now, monotonic_now, ig.watching and ig.interval == 0)
    out = [line(("▸ " if focused else "", H1), (ig.title, H1), status)]
    if err:
        out.append(line((f"⚠ {err}", BAD)))
    out += rows
    if ig.keys:
        out.append(line((" · ".join(f"{key} {action}" for key, action in ig.keys.items()), META)))
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
    interval = 60
    show_usage = True
    show_review = True
    show_pace = True
    excluded = []
    integrations = []
    width = 80  # the pane width at the last render, passed to integrations as COLUMNS
    keys = DEFAULTS["keys"]
    focus = None
    scroll = 0  # rows hidden above the window when the dashboard is taller than the pane
    live = None
    global_next_fetch = None
    prs_fetching = False
    usage_fetching = False


class Dashboard:
    """Rebuilt on every Live refresh, so resizes and countdowns stay current.

    With `window`, a dashboard taller than the pane shows the rows at `st.scroll` beside a scrollbar."""

    def __init__(self, st, window=True):
        self.st = st
        self.window = window

    def __rich_console__(self, console, options):
        now = time.time()
        monotonic_now = time.monotonic()
        st = self.st
        keys = st.keys
        room = max(1, (options.height or console.height) - 2)
        width = options.max_width
        rows = self.rows(width, now, monotonic_now)
        if self.window and len(rows) > room:  # lay out one column narrower to make room for the scrollbar
            width -= 1
            rows = self.rows(width, now, monotonic_now)
        overflow = self.window and len(rows) > room  # data can change between the two layouts
        if width != st.width:  # integrations lay out for the new width now, not at their next run
            st.width = width
            for ig in st.integrations:
                ig.request_refresh(after_current=True)
        hints = [f"{keys['quit']} quit"] if keys["quit"] else []
        if len(focusable_integrations(st)) >= 2 and keys["focus"]:
            hints.append(f"{keys['focus']} focus")
        scroll_keys = "/".join(k for k in (keys["scroll_up"], keys["scroll_down"]) if k)
        if overflow and scroll_keys:
            hints.append(f"{scroll_keys} scroll")
        # The key loop changes st.scroll on another thread: read it once, clamp it, and use only that.
        scroll = max(0, min(st.scroll, len(rows) - room)) if overflow else 0
        st.scroll = scroll
        if overflow:
            rows = [with_bar(row, width, bar)
                    for row, bar in zip(rows[scroll:scroll + room],
                                        scrollbar(room, len(rows), scroll), strict=True)]
        yield Group(*rows, Text(), line((" · ".join(hints), META)))

    def rows(self, width, now, monotonic_now):
        st = self.st
        keys = st.keys
        slot = {p: [render_integration(ig, now, ig is st.focus, monotonic_now)
                    for ig in st.integrations if ig.position == p]
                for p in POSITIONS}
        mine = render_mine(st, width, now, monotonic_now)
        review = render_review(st, width, now, monotonic_now) if st.show_review else None
        if keys["refresh"]:  # the hint sits under the last PR section
            (review if review is not None else mine).append(line((f"{keys['refresh']} refresh", META)))
        sections = slot["top"] + ([render_usage(st, width, now, monotonic_now)] if st.show_usage else [])
        sections += slot["after-usage"] + [mine] + slot["after-my-prs"]
        sections += ([review] if review is not None else []) + slot["bottom"]
        rows = list(sections[0])
        for rows_of in sections[1:]:
            rows += [Text(), rule(width), *rows_of]
        return rows


def scrollbar(room, total, scroll):
    """One character per visible row: a thumb sized and placed by the visible share of `total`."""
    thumb = max(1, room * room // total)
    start = round(scroll * (room - thumb) / (total - room))
    return ["█" if start <= i < start + thumb else "│" for i in range(room)]


def with_bar(row, width, bar):
    """`row` cut or padded to `width` cells, then the scrollbar character."""
    row = row.copy()
    row.truncate(width, overflow="ellipsis", pad=True)
    row.append(bar, RULE if bar == "│" else META)
    return row


def refresh(st, usage_every, force):
    st.prs_fetching = True
    now = time.time()
    invalidate = False
    if st.show_usage:
        st.usage_fetching = True
        # Other omp sessions keep omp's shared cache fresh, so force a refetch only
        # when the oldest live read is older than usage_every, and never twice in a
        # minute: every forced call is another hit on rate-limited usage endpoints.
        since = now - st.usage_inval_at
        age = oldest_read(st.usage)
        invalidate = (usage_every is not None and since >= 60
                      and (force or age is None or age >= usage_every))

    results = queue.Queue()

    def fetch(section, function, *args):
        try:
            results.put((section, function(*args), None))
        except Exception as e:
            results.put((section, None, e))

    threading.Thread(target=fetch, args=("prs", fetch_prs, st.excluded, st.show_review, st.gh_account),
                     daemon=True, name="devdash-prs").start()
    sections = 1
    if st.show_usage:
        threading.Thread(target=fetch, args=("usage", fetch_usage, invalidate, st.omp_profile),
                         daemon=True, name="devdash-usage").start()
        sections += 1
    if st.live is not None:
        st.live.refresh()
    for _ in range(sections):
        section, value, error = results.get()
        try:
            if error is not None:
                raise error
            if section == "usage":
                st.usage, st.usage_at, st.usage_err = carry_over(st.usage, value), time.time(), None
                if invalidate:
                    st.usage_inval_at = now
                save_last_good(st.usage, st.cache_profile)
            else:
                (st.mine, st.review), st.prs_at, st.prs_err = value, time.time(), None
        except Exception as e:  # keep the last good data on screen
            if section == "usage":
                st.usage_err = str(e)[:80]
            else:
                st.prs_err = str(e)[:80]
        finally:
            if section == "usage":
                st.usage_fetching = False
            else:
                st.prs_fetching = False
            if st.live is not None:
                st.live.refresh()
    if st.live is not None:
        st.live.refresh()


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
    ap.add_argument("--no-integration", action="append", default=[], metavar="NAME",
                    help="hide the custom integration NAME; repeatable")
    ap.add_argument("--no-integrations", action="store_true", help="hide every custom integration")
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
    st.keys = cfg["keys"]
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
    hidden = set(args.no_integration)
    unknown = hidden - {s["name"] for s in cfg["integrations"]}
    if unknown:
        raise SystemExit(f"devdash: --no-integration: no integration named '{sorted(unknown)[0]}'")
    if not args.no_integrations:
        st.integrations = [Integration(s, st.interval) for s in cfg["integrations"]
                           if s["enabled"] and s["name"] not in hidden]

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
    st.width = CONSOLE.width
    # Closing the pane sends SIGHUP; exit through `finally` so running integrations are killed.
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(0))
    try:
        if args.once or not sys.stdin.isatty():
            print_once(st, usage_every)
        else:
            watch(st, usage_every)
    except KeyboardInterrupt:
        pass
    finally:
        for ig in st.integrations:
            ig.stop()


def print_once(st, usage_every):
    runner = ThreadPoolExecutor(max(1, len(st.integrations)))
    try:
        jobs = [runner.submit(ig.poll, st.width) for ig in st.integrations]
        refresh(st, usage_every, False)
        for job in jobs:
            job.result()
    finally:
        runner.shutdown(wait=False)  # on an early exit, main stops the commands these workers wait on
    CONSOLE.print(Dashboard(st, window=False))


def watch(st, usage_every):
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    # Alternate scroll mode: the mouse wheel sends arrow keys on the full-screen view, which
    # has no scrollback, so the wheel scrolls the dashboard. Save the mode and restore it on exit.
    scroll_mode = CONSOLE.is_terminal
    try:
        if scroll_mode:
            CONSOLE.file.write("\x1b[?1007s\x1b[?1007h")
            CONSOLE.file.flush()
        with integration_state_files(st):
            focusable = focusable_integrations(st)
            st.focus = focusable[0] if focusable else None
            try:
                live = Live(Dashboard(st), console=CONSOLE, screen=True, refresh_per_second=1)
                with live:
                    st.live = live
                    for ig in st.integrations:
                        threading.Thread(target=ig.loop, args=(st,), daemon=True,
                                         name=f"integration-{ig.name}").start()
                    pending = b""
                    pending_since = None
                    force = True
                    refresh_thread = None
                    deadline = time.monotonic()
                    while True:
                        now = time.monotonic()
                        if refresh_thread is None or not refresh_thread.is_alive():
                            if force or now >= deadline:
                                # An r press during a refresh requests one follow-up run; it is not lost.
                                run_force = force
                                force = False
                                st.prs_fetching = True
                                st.usage_fetching = st.show_usage
                                deadline = now + st.interval
                                st.global_next_fetch = deadline
                                refresh_thread = threading.Thread(
                                    target=refresh, args=(st, usage_every, run_force),
                                    daemon=True, name="devdash-refresh")
                                refresh_thread.start()
                                live.refresh()
                        timeout = max(0, deadline - time.monotonic())
                        if refresh_thread.is_alive() and (force or timeout == 0):
                            # poll so a pending follow-up starts as soon as this refresh ends
                            timeout = 0.2 if timeout == 0 else min(timeout, 0.2)
                        if pending_since is not None:
                            timeout = min(timeout, max(0, pending_since + KEY_ESCAPE_TIMEOUT -
                                                       time.monotonic()))
                        if select.select([fd], [], [], timeout)[0]:
                            available = bytearray()
                            while select.select([fd], [], [], 0)[0]:
                                chunk = os.read(fd, 65536)
                                if not chunk:
                                    break
                                available.extend(chunk)
                            keys, pending = decode_keys(bytes(available), pending)
                            pending_since = time.monotonic() if pending else None
                            for key in keys:
                                outcome = dispatch_key(st, key)
                                if outcome == "quit":
                                    return
                                if outcome == "refresh":
                                    force = True
                        elif pending_since is not None and time.monotonic() >= pending_since + KEY_ESCAPE_TIMEOUT:
                            _, pending = decode_keys(b"", pending, flush=True)
                            pending_since = None
            finally:
                st.live = None
                for ig in st.integrations:
                    ig.stop()
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        finally:
            if scroll_mode:
                CONSOLE.file.write("\x1b[?1007r")
                CONSOLE.file.flush()


if __name__ == "__main__":
    main()
