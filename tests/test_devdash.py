import json

import pytest

import devdash


def pr(number, head, base, repo="acme/web", stack=None):
    return {"number": number, "title": f"feat: change {number}", "url": f"https://github.com/{repo}/pull/{number}",
            "headRefName": head, "baseRefName": base, "repository": {"nameWithOwner": repo}, "stack": stack}


@pytest.mark.parametrize(("repo", "excluded"), [
    ("acme/web", True),        # exact repository
    ("ACME/Web", True),        # GitHub names are case-insensitive
    ("acme/api", True),        # owner rule covers every repository of the owner
    ("acme-labs/web", False),  # an owner rule is not a prefix match
    ("other/web", False),
])
def test_is_excluded(repo, excluded):
    assert devdash.is_excluded(repo, ["acme/web", "acme"]) is excluded


def test_is_excluded_repo_rule_keeps_owner_siblings():
    assert not devdash.is_excluded("acme/api", ["acme/web"])


def load(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return devdash.load_config(str(path), explicit=True)


def test_config_merges_over_defaults(tmp_path):
    cfg = load(tmp_path, '[github]\nexclude = ["acme"]\n')
    assert cfg["github"] == {"exclude": ["acme"], "review_requested": True}
    assert cfg["interval"] == 60


@pytest.mark.parametrize("text", [
    "intervl = 5\n",                          # typo in a top-level key
    "[github]\nexclude = \"acme\"\n",         # string where a list belongs
    "[github]\nreview_requested = 1\n",       # number where a boolean belongs
    "interval = true\n",                      # boolean where a number belongs
    "[usage]\nenabled = \"sometimes\"\n",
])
def test_config_rejects_mistakes(tmp_path, text):
    with pytest.raises(SystemExit):
        load(tmp_path, text)


def test_missing_config_is_an_error_only_when_named(tmp_path):
    assert devdash.load_config(str(tmp_path / "none.toml"), explicit=False) == devdash.DEFAULTS
    with pytest.raises(SystemExit):
        devdash.load_config(str(tmp_path / "none.toml"), explicit=True)


@pytest.mark.parametrize("rule", ["a b", "acme/web/x", 'acme" is:closed', ""])
def test_exclude_rules_cannot_inject_search_qualifiers(rule):
    with pytest.raises(SystemExit):
        devdash.check_rules([rule])


def test_branch_chain_becomes_one_stack_top_first():
    prs = [pr(1, "a", "main"), pr(2, "b", "a"), pr(3, "c", "b"), pr(9, "solo", "main")]
    groups = devdash.group_mine(prs)["acme/web"]
    chain = next(g for g in groups if g[0] == "stack")
    assert chain[1] == "chain → main"
    assert [p["number"] for p in chain[2]] == [3, 2, 1]
    assert [g[1]["number"] for g in groups if g[0] == "single"] == [9]


def test_fork_in_a_chain_is_not_merged_into_one_stack():
    prs = [pr(1, "a", "main"), pr(2, "b", "a"), pr(3, "c", "a")]
    groups = devdash.group_mine(prs)["acme/web"]
    assert all(g[0] == "single" for g in groups)


def test_native_stack_includes_layers_that_are_not_mine():
    stack = {"number": 4, "baseRefName": "main", "entries": {"nodes": [
        {"position": 1, "pullRequest": {"number": 10, "state": "MERGED", "title": "t", "url": "u"}},
        {"position": 2, "pullRequest": {"number": 11, "state": "OPEN", "title": "t", "url": "u"}},
    ]}}
    groups = devdash.group_mine([pr(11, "b", "a", stack=stack)])["acme/web"]
    assert groups[0][0] == "stack"
    assert [p["number"] for p in groups[0][2]] == [11, 10]


def test_fetch_retries_without_stack_field_when_schema_lacks_it(monkeypatch):
    monkeypatch.setattr(devdash, "HAS_STACK", True)
    queries = []

    def fake_run(cmd, timeout=45):
        query = cmd[-1]
        queries.append(query)
        if "stack {" in query:
            raise RuntimeError("gh: Field 'stack' doesn't exist on type 'PullRequest'")
        nodes = [pr(1, "a", "main"), pr(2, "b", "main", repo="acme/hidden")]
        return json.dumps({"data": {"mine": {"nodes": nodes}}})

    monkeypatch.setattr(devdash, "run", fake_run)
    mine, review = devdash.fetch_prs(["acme/hidden"], review_requested=False)
    assert [p["number"] for p in mine] == [1]
    assert review == []
    assert "-repo:acme/hidden" in queries[-1]
    assert "stack {" not in queries[-1]
    devdash.fetch_prs([], review_requested=False)
    assert len(queries) == 3  # the fallback is remembered: no second failed attempt


def test_fetch_does_not_hide_other_errors(monkeypatch):
    monkeypatch.setattr(devdash, "HAS_STACK", True)

    def fake_run(cmd, timeout=45):
        raise RuntimeError("gh: HTTP 401: Bad credentials")

    monkeypatch.setattr(devdash, "run", fake_run)
    with pytest.raises(RuntimeError, match="401"):
        devdash.fetch_prs([], review_requested=True)


DAY = 86400
NOW = 1_800_000_000


def limit(window, used, left, duration=None):
    win = {"id": window, "resetsAt": (NOW + left) * 1000}
    if duration:
        win["durationMs"] = duration * 1000
    return {"label": "x", "window": win, "amount": {"usedFraction": used}}


def test_monthly_window_starts_one_calendar_month_before_reset():
    from datetime import UTC, datetime
    reset = datetime(2026, 3, 31, 12, tzinfo=UTC).timestamp()
    start = devdash.window_start({"id": "monthly", "resetsAt": reset * 1000})
    assert datetime.fromtimestamp(start, UTC) == datetime(2026, 2, 28, 12, tzinfo=UTC)


def test_pace_is_points_used_ahead_of_time():
    # 20% used with 4 of 7 days gone: 37 points behind. 60% with 3 of 7 gone: 17 ahead.
    assert devdash.pace(limit("7d", 0.20, 3 * DAY), NOW) == -37
    assert devdash.pace(limit("7d", 0.60, 4 * DAY, duration=7 * DAY), NOW) == 17


@pytest.mark.parametrize(("points", "icon"), [
    (0, "|"), (4, "|"), (-4, "|"),
    (5, ">"), (15, ">>"), (29, ">>"), (30, ">>>"),
    (-5, "<"), (-15, "<<"), (-30, "<<<"),
])
def test_pace_icon_steps(points, icon):
    assert devdash.pace_icon(points).plain == icon


@pytest.mark.parametrize("lim", [
    limit("5h", 0.9, 3600),          # not a multi-day window
    limit("7d", 0.05, 7 * DAY - 3600),  # an hour in: too early to judge
    limit("mystery", 0.5, DAY),      # no way to know when the window began
    dict(limit("7d", 1.0, 3 * DAY), status="exhausted"),  # nothing left to pace
])
def test_pace_hidden(lim):
    assert devdash.pace(lim, NOW) is None



@pytest.mark.parametrize("show", [True, False])
def test_usage_row_shows_pace_icon_unless_turned_off(show):
    st = devdash.State()
    st.show_pace = show
    st.usage = {"reports": [{"provider": "cursor", "fetchedAt": NOW * 1000,
                             "limits": [dict(limit("monthly", 0.34, 11 * DAY), label="Cursor Models")]}]}
    row = devdash.render_usage(st, 50, NOW)[-1].plain
    assert ("<<" in row) is show


@pytest.mark.parametrize(("points", "row"), [
    (None, "        34%         "),
    (0,    "        34% |       "),
    (20,   "        34% >>      "),
    (-20,  "     << 34%         "),
])
def test_pace_icon_sits_beside_a_value_that_never_moves(points, row):
    icon = devdash.pace_icon(points) if points is not None else None
    assert devdash.meter(0.0, 20, "34%", icon=icon).plain == row
