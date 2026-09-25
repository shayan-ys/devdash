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
    assert cfg["github"] == {"exclude": ["acme"], "review_requested": True, "account": ""}
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


def test_usage_profile_selects_omp_auth_and_isolates_last_good(tmp_path, monkeypatch):
    monkeypatch.setattr(devdash, "LAST_GOOD", str(tmp_path / "last-usage.json"))
    monkeypatch.delenv("OMP_PROFILE", raising=False)
    devdash.save_last_good({"reports": [{"provider": "anthropic"}]})
    devdash.save_last_good({"reports": [{"provider": "cursor"}]}, "personal")
    assert [r["provider"] for r in devdash.load_last_good()["reports"]] == ["anthropic"]
    assert [r["provider"] for r in devdash.load_last_good("personal")["reports"]] == ["cursor"]

    commands = []
    monkeypatch.setattr(devdash.subprocess, "run", lambda cmd, **kw: commands.append(cmd))
    monkeypatch.setattr(devdash, "fetch_openrouter", lambda profile: None)
    monkeypatch.setattr(devdash, "fetch_nous_portal", lambda profile: None)

    def fake_run(cmd, timeout=45, env=None):
        commands.append(cmd)
        return json.dumps({"reports": [{"provider": "cursor"}], "accountsWithoutUsage": []})

    monkeypatch.setattr(devdash, "run", fake_run)
    data = devdash.carry_over(devdash.load_last_good("personal"), devdash.fetch_usage(True, "personal"))
    assert [r["provider"] for r in data["reports"]] == ["cursor"]
    assert commands == [
        ["omp", "--profile", "personal", "usage", "invalidate"],
        ["omp", "--profile", "personal", "usage", "--json"],
    ]


def test_openrouter_reads_selected_profile_key_usage_without_storing_credential(monkeypatch):
    from io import BytesIO
    from subprocess import CompletedProcess

    commands = []

    def fake_token(cmd, **kwargs):
        commands.append(cmd)
        return CompletedProcess(cmd, 0, stdout="private-key\n")

    def fake_urlopen(request, timeout):
        assert request.get_header("Authorization") == "Bearer private-key"
        if request.full_url == "https://openrouter.ai/api/v1/key":
            return BytesIO(b'{"data":{"usage":99,"usage_monthly":4,"limit":10,"limit_reset":"monthly"}}')
        if request.full_url == "https://openrouter.ai/api/v1/credits":
            return BytesIO(b'{"data":{"total_credits":12,"total_usage":2}}')
        raise AssertionError(request.full_url)

    monkeypatch.setattr(devdash.subprocess, "run", fake_token)
    monkeypatch.setattr(devdash, "urlopen", fake_urlopen)
    report = devdash.fetch_openrouter("personal")
    assert commands == [["omp", "--profile", "personal", "token", "openrouter"]]
    assert report["limits"][0]["amount"] == {"used": 4.0, "limit": 10.0, "usedFraction": 0.4, "unit": "usd"}
    assert report["limits"][1]["amount"] == {"used": 10.0, "unit": "usd"}
    assert "private-key" not in json.dumps(report)


def test_openrouter_uncapped_zero_usage_is_visible_and_failed_auth_is_hidden(monkeypatch):
    from io import BytesIO
    from subprocess import CompletedProcess

    monkeypatch.setattr(devdash.subprocess, "run",
                        lambda cmd, **kw: CompletedProcess(cmd, 0, stdout="private-key"))
    monkeypatch.setattr(devdash, "urlopen",
                        lambda request, timeout: BytesIO(b'{"data":{"usage":0,"limit":null}}'))
    report = devdash.fetch_openrouter(None)
    st = devdash.State()
    st.usage = {"reports": [report]}
    assert "$0.00" in "\n".join(row.plain for row in devdash.render_usage(
        st, 50, report["fetchedAt"] / 1000))
    monkeypatch.setattr(devdash.subprocess, "run",
                        lambda cmd, **kw: CompletedProcess(cmd, 1, stdout=""))
    assert devdash.fetch_openrouter("personal") is None


def test_nous_portal_reads_subscription_credits_from_omp_token(monkeypatch):
    from io import BytesIO
    from subprocess import CompletedProcess

    commands = []

    def fake_token(cmd, **kwargs):
        commands.append(cmd)
        provider = cmd[-1]
        if provider == "nous-portal":
            return CompletedProcess(cmd, 0, stdout="portal-token\n")
        return CompletedProcess(cmd, 1, stdout="")

    def fake_urlopen(request, timeout):
        assert request.full_url == "https://portal.nousresearch.com/api/oauth/account"
        assert request.get_header("Authorization") == "Bearer portal-token"
        return BytesIO(b'''{"subscription":{"monthly_credits":100,"credits_remaining":40,
            "current_period_end":"2026-10-01T00:00:00Z"},
            "paid_service_access":{"total_usable_credits":55,"purchased_credits_remaining":15}}''')

    monkeypatch.setattr(devdash.subprocess, "run", fake_token)
    monkeypatch.setattr(devdash, "urlopen", fake_urlopen)
    report = devdash.fetch_nous_portal("personal")
    assert commands == [["omp", "--profile", "personal", "token", "nous-portal"]]
    labels = [lim["label"] for lim in report["limits"]]
    assert labels == ["subscription", "credits left", "top-up"]
    assert report["limits"][0]["amount"]["usedFraction"] == 0.6
    assert report["limits"][0]["window"]["id"] == "monthly"
    assert "portal-token" not in json.dumps(report)
    st = devdash.State()
    st.usage = {"reports": [report]}
    text = "\n".join(row.plain for row in devdash.render_usage(st, 60, report["fetchedAt"] / 1000))
    assert "Nous Portal" in text
    monkeypatch.setattr(devdash.subprocess, "run",
                        lambda cmd, **kw: CompletedProcess(cmd, 1, stdout=""))
    assert devdash.fetch_nous_portal("personal") is None


def test_omp_profile_environment_selects_cache_without_explicit_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(devdash, "LAST_GOOD", str(tmp_path / "last-usage.json"))
    monkeypatch.delenv("OMP_PROFILE", raising=False)
    devdash.save_last_good({"reports": [{"provider": "anthropic"}]})
    monkeypatch.setenv("OMP_PROFILE", "personal")
    devdash.save_last_good({"reports": [{"provider": "cursor"}]})
    assert [r["provider"] for r in devdash.load_last_good()["reports"]] == ["cursor"]
    monkeypatch.delenv("OMP_PROFILE")
    assert [r["provider"] for r in devdash.load_last_good()["reports"]] == ["anthropic"]


def test_github_env_selects_named_login_without_switching_active_account(monkeypatch):
    from subprocess import CompletedProcess
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        assert "token" not in (kwargs.get("env") or {})
        return CompletedProcess(cmd, 0, stdout="gh-token\n")

    monkeypatch.setattr(devdash.subprocess, "run", fake_run)
    env = devdash.github_env("alice")
    assert commands == [["gh", "auth", "token", "-u", "alice"]]
    assert env["GH_TOKEN"] == env["GITHUB_TOKEN"] == "gh-token"
    assert devdash.github_env("") is None
    assert devdash.github_env(None) is None


def test_fetch_prs_pass_named_account_to_gh(monkeypatch):
    envs = []

    def fake_run(cmd, timeout=45, env=None):
        envs.append(env)
        return json.dumps({"data": {"mine": {"nodes": []}, "review": {"nodes": []}}})

    monkeypatch.setattr(devdash, "github_env", lambda account: {"GH_TOKEN": f"for-{account}"})
    monkeypatch.setattr(devdash, "run", fake_run)
    devdash.fetch_prs([], review_requested=True, account="alice")
    assert envs == [{"GH_TOKEN": "for-alice"}]


def test_profile_shows_provider_email_in_white_next_to_name():
    st = devdash.State()
    st.omp_profile = "personal"
    st.me = "alice"
    st.usage = {"reports": [{"provider": "openai-codex", "limits": [],
                             "metadata": {"email": "user@example.com"}}]}
    row = next(r.plain for r in devdash.render_usage(st, 50, 0) if "Codex" in r.plain)
    assert "Codex (user@example.com)" in row
    assert "alice" not in row
    st.omp_profile = None
    row = next(r.plain for r in devdash.render_usage(st, 50, 0) if "Codex" in r.plain)
    assert "user@example.com" not in row


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

    def fake_run(cmd, timeout=45, env=None):
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


def rollup(*nodes, state="FAILURE"):
    return {"commits": {"nodes": [{"commit": {"statusCheckRollup": {
        "state": state, "contexts": {"nodes": list(nodes)}}}}]}}


def check(name, conclusion, status="COMPLETED", database_id=1, started="2026-09-25T16:00:00Z"):
    return {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion, "databaseId": database_id, "startedAt": started}


def test_ci_badge_uses_the_latest_run_of_a_check_name():
    """A cancelled workflow posts Verify / gate FAILURE; the surviving run posts SUCCESS.
    statusCheckRollup.state stays FAILURE because every run remains on the SHA."""
    pr = rollup(
        check("Verify / gate", "FAILURE", database_id=1, started="2026-09-25T15:57:07Z"),
        check("Verify / gate", "SUCCESS", database_id=2, started="2026-09-25T16:04:12Z"),
        check("Infra / gate", "FAILURE", database_id=3, started="2026-09-25T15:57:07Z"),
        check("Infra / gate", "SUCCESS", database_id=4, started="2026-09-25T15:57:32Z"),
        check("Infra / plan (${{ matrix.env }})", "CANCELLED", database_id=5),
        {"__typename": "StatusContext", "context": "CodeRabbit", "state": "SUCCESS",
         "createdAt": "2026-09-25T16:00:00Z"},
    )
    badge, failed = devdash.ci_badge(pr)
    assert badge.plain == "✓ci"
    assert failed == ""


def test_ci_badge_a_later_failure_replaces_an_earlier_pass():
    pr = rollup(
        check("Verify / gate", "SUCCESS", database_id=1),
        check("Verify / gate", "FAILURE", database_id=2),
    )
    badge, failed = devdash.ci_badge(pr)
    assert badge.plain == "✗ci1"
    assert failed == "Verify / gate"


def test_ci_badge_a_rerun_in_progress_is_pending_not_the_old_result():
    pr = rollup(
        check("Verify / gate", "SUCCESS", database_id=1),
        check("Verify / gate", None, status="IN_PROGRESS", database_id=2),
    )
    badge, failed = devdash.ci_badge(pr)
    assert badge.plain == "●ci1"
    assert failed == ""


def test_fetch_does_not_hide_other_errors(monkeypatch):
    monkeypatch.setattr(devdash, "HAS_STACK", True)

    def fake_run(cmd, timeout=45, env=None):
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



def test_usage_hides_disabled_credentials_but_shows_authenticated_accounts_without_data():
    st = devdash.State()
    st.usage = {"reports": [{"provider": "cursor", "limits": []}],
                "accountsWithoutUsage": [{"provider": "openai-codex"}],
                "disabledCredentials": [{"provider": "anthropic"}]}
    text = "\n".join(row.plain for row in devdash.render_usage(st, 50, NOW))
    assert "Cursor" in text
    assert "no data: Codex" in text
    assert "Anthropic" not in text
    assert "disabled credential" not in text


@pytest.mark.parametrize(("points", "row"), [
    (None, "        34%         "),
    (0,    "        34% |       "),
    (20,   "        34% >>      "),
    (-20,  "     << 34%         "),
])
def test_pace_icon_sits_beside_a_value_that_never_moves(points, row):
    icon = devdash.pace_icon(points) if points is not None else None
    assert devdash.meter(0.0, 20, "34%", icon=icon).plain == row
