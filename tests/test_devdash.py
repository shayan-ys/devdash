import io
import json
import os
import sys
import time

import pytest
from rich.console import Console

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
    "[[integrations]]\ncommand = [\"date\"]\n",                                    # no name
    "[[integrations]]\nname = \"a b\"\ncommand = [\"date\"]\n",                     # name unusable as a flag value
    "[[integrations]]\nname = \"a\\n\"\ncommand = [\"date\"]\n",                    # trailing newline
    "[[integrations]]\nname = \"a\"\ncommand = []\n",
    "[[integrations]]\nname = \"a\"\ncommand = \"date\"\n",                        # a shell string, not argv
    "[[integrations]]\nname = \"a\"\ncommand = [\"date\"]\nposition = \"middle\"\n",
    "[[integrations]]\nname = \"a\"\ncommand = [\"date\"]\ntimeout = 0\n",
    "[[integrations]]\nname = \"a\"\ncommand = [\"date\"]\ntimeout = 9223372036854775807\n",  # too big to wait on
    "[[integrations]]\nname = \"a\"\ncommand = [\"date\"]\ninterval = 9223372036854775807\n",
    "[[integrations]]\nname = \"a\"\ncommand = [\"date\"]\nenv = { N = 1 }\n",
    "[[integrations]]\nname = \"a\"\ncommand = [\"date\"]\nintervl = 5\n",
    "[[integrations]]\nname = \"a\"\ncommand = [\"date\"]\n[[integrations]]\nname = \"a\"\ncommand = [\"date\"]\n",
])
def test_config_rejects_mistakes(tmp_path, text):
    with pytest.raises(SystemExit):
        load(tmp_path, text)



def test_key_config_accepts_case_sensitive_names_and_records_an_empty_table(tmp_path):
    cfg = load(tmp_path, '[keys]\nrefresh = "R"\nquit = ""\nfocus = "tab"\n'
                        '[[integrations]]\nname = "t"\ncommand = ["date"]\nkeys = {}\n')
    assert cfg["keys"] == {"refresh": "R", "quit": "", "focus": "tab"}
    assert cfg["integrations"][0]["keys"] == {}
    assert cfg["integrations"][0]["has_keys"] is True


@pytest.mark.parametrize("text", [
    '[keys]\nrefresh = " "\n',
    '[keys]\nrefresh = "r"\nquit = "r"\n',
    '[keys]\nrefresh = 1\n',
    '[keys]\nunknown = "x"\n',
    '[[integrations]]\nname = "t"\ncommand = ["date"]\nkeys = 1\n',
    '[[integrations]]\nname = "t"\ncommand = ["date"]\nkeys = { " " = "newer" }\n',
    '[[integrations]]\nname = "t"\ncommand = ["date"]\nkeys = { j = "" }\n',
    '[[integrations]]\nname = "t"\ncommand = ["date"]\nkeys = { j = 7 }\n',
    '[[integrations]]\nname = "t"\ncommand = ["date"]\nkeys = { "upwards" = "newer" }\n',
])
def test_key_config_rejects_invalid_shapes_and_names(tmp_path, text):
    with pytest.raises(SystemExit):
        load(tmp_path, text)


def test_builtin_binding_conflict_names_key_integration_and_remapping(tmp_path):
    text = ('[[integrations]]\nname = "t"\ncommand = ["date"]\nenabled = false\n'
            'keys = { r = "newer" }\n')
    with pytest.raises(SystemExit) as error:
        load(tmp_path, text)
    assert "integration 't'" in str(error.value)
    assert "key 'r'" in str(error.value)
    assert "[keys]" in str(error.value)


def test_decode_keys_preserves_case_maps_special_keys_and_ignores_other_escapes():
    decoded, pending = devdash.decode_keys(
        b"rR\t\r\n\x1b[A\x1bOB\x1b[C\x1bOD\x1b[1;5A\x1b[Z\x1bOP\x1b")
    assert decoded == ["r", "R", "tab", "enter", "enter", "up", "down", "right", "left"]
    assert pending == b"\x1b"
    assert devdash.decode_keys(b"", pending, flush=True) == ([], b"")


def test_decode_keys_keeps_partial_sequences_until_the_next_read():
    decoded, pending = devdash.decode_keys(b"j\x1b[")
    assert decoded == ["j"] and pending == b"\x1b["
    decoded, pending = devdash.decode_keys(b"A", pending)
    assert decoded == ["up"] and pending == b""



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


@pytest.mark.parametrize("cached", [None, {"reports": [{"provider": "cursor", "old": True}]}])
def test_refresh_replaces_cached_usage_with_the_fresh_report(cached, monkeypatch):
    fresh = {"reports": [{"provider": "cursor"}], "accountsWithoutUsage": []}
    monkeypatch.setattr(devdash, "fetch_usage", lambda invalidate, profile: fresh)
    monkeypatch.setattr(devdash, "fetch_prs", lambda *args: ([], []))
    monkeypatch.setattr(devdash, "save_last_good", lambda data, profile: None)
    st = devdash.State()
    st.usage = cached
    devdash.refresh(st, None, False)
    assert st.usage_err is None
    assert st.usage["reports"] == [{"provider": "cursor"}]


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
    monkeypatch.setattr(devdash, "HAS_WORKFLOW_RUN", True)
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


def check(name, conclusion, status="COMPLETED", database_id=1, started="2026-09-25T16:00:00Z",
          workflow=None, run=None, created="2026-09-25T16:00:00Z"):
    node = {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion, "databaseId": database_id, "startedAt": started}
    if workflow and run:
        node["checkSuite"] = {"workflowRun": {
            "databaseId": run, "createdAt": created, "workflow": {"name": workflow}}}
    return node


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


def test_ci_badge_ignores_gate_failure_from_an_older_workflow_run():
    """Cancelled Verify run posts gate FAILURE; the new Verify run is still in
    progress and has not posted gate yet."""
    pr = rollup(
        check("Verify / gate", "FAILURE", database_id=1, started="2026-09-25T20:38:05Z",
              workflow="Verify", run=10, created="2026-09-25T20:38:00Z"),
        check("Verify / clients", "CANCELLED", database_id=2, started="2026-09-25T20:38:01Z",
              workflow="Verify", run=10, created="2026-09-25T20:38:00Z"),
        check("Verify / clients", None, status="IN_PROGRESS", database_id=3,
              started="2026-09-25T20:38:29Z",
              workflow="Verify", run=20, created="2026-09-25T20:38:14Z"),
        check("Verify / ios / compile", None, status="IN_PROGRESS", database_id=4,
              started="2026-09-25T20:38:32Z",
              workflow="Verify", run=20, created="2026-09-25T20:38:14Z"),
        check("Infra / gate", "FAILURE", database_id=5, started="2026-09-25T20:38:04Z",
              workflow="Infra", run=11, created="2026-09-25T20:38:00Z"),
        check("Infra / gate", "SUCCESS", database_id=6, started="2026-09-25T20:38:37Z",
              workflow="Infra", run=21, created="2026-09-25T20:38:14Z"),
        {"__typename": "StatusContext", "context": "CodeRabbit", "state": "SUCCESS",
         "createdAt": "2026-09-25T20:38:23Z"},
    )
    badge, failed = devdash.ci_badge(pr)
    assert badge.plain == "●ci2"
    assert failed == ""


def test_ci_badge_same_workflow_run_failure_is_still_failure():
    pr = rollup(
        check("Verify / gate", "FAILURE", database_id=1, workflow="Verify", run=20),
        check("Verify / clients", None, status="IN_PROGRESS", database_id=2,
              workflow="Verify", run=20),
    )
    badge, failed = devdash.ci_badge(pr)
    assert badge.plain == "✗ci1"
    assert failed == "Verify / gate"


def test_fetch_retries_without_workflow_run_when_schema_lacks_it(monkeypatch):
    monkeypatch.setattr(devdash, "HAS_STACK", False)
    monkeypatch.setattr(devdash, "HAS_WORKFLOW_RUN", True)
    queries = []

    def fake_run(cmd, timeout=45, env=None):
        query = cmd[-1]
        queries.append(query)
        if "workflowRun" in query:
            raise RuntimeError("gh: Field 'workflowRun' doesn't exist on type 'CheckSuite'")
        nodes = [pr(1, "a", "main")]
        return json.dumps({"data": {"mine": {"nodes": nodes}}})

    monkeypatch.setattr(devdash, "run", fake_run)
    mine, review = devdash.fetch_prs([], review_requested=False)
    assert [p["number"] for p in mine] == [1]
    assert "workflowRun" not in queries[-1]
    assert "... on CheckRun { name status conclusion databaseId startedAt }" in queries[-1]
    devdash.fetch_prs([], review_requested=False)
    assert len(queries) == 3


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


# ── custom integrations ───────────────────────────────────────────────────────
def integration(code, **spec):
    """An integration whose command is a Python snippet, so tests need no shell tools."""
    return devdash.Integration({**devdash.INTEGRATION, "name": "t", **spec,
                                "command": [sys.executable, "-c", code]}, 60)


def gone(pid, wait=5):
    """True once `pid` has exited within `wait` seconds; a zombie waiting to be reaped counts as exited."""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        state = devdash.subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        if not state.stdout.strip() or state.stdout.strip().startswith("Z"):
            return True
        time.sleep(0.05)
    return False


def spawner(pid_file, sleep=60):
    """Python that starts a sleeping child, records the child's pid, then sleeps itself."""
    return ("import subprocess, sys, time\n"
            f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({sleep})'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
            f"time.sleep({sleep})")


def wait_for_pid(pid_file):
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text():
            return int(pid_file.read_text())
        time.sleep(0.05)
    pytest.fail("the command never started its child")


def test_integration_action_and_state_environment_are_scoped_to_the_run(tmp_path, monkeypatch):
    code = ("import json, os; print(json.dumps({"
            "'action': os.environ.get('DEVDASH_ACTION'), "
            "'state': os.environ.get('DEVDASH_STATE_FILE')}))")
    ig = integration(code, keys={"j": "newer"})
    monkeypatch.setenv("DEVDASH_ACTION", "inherited")
    monkeypatch.setenv("DEVDASH_STATE_FILE", "inherited")
    ig.poll(40)
    assert json.loads(ig.result[0][0].plain) == {"action": None, "state": None}

    state_file = tmp_path / "state.json"
    state_file.write_text("")
    ig.state_file = str(state_file)
    ig.poll(40, action="newer")
    assert json.loads(ig.result[0][0].plain) == {
        "action": "newer", "state": str(state_file)}


def test_watch_state_files_are_empty_private_and_removed(tmp_path):
    cfg = load(tmp_path, '[[integrations]]\nname = "t"\ncommand = ["date"]\nkeys = {}\n')
    st = devdash.State()
    st.integrations = [devdash.Integration(cfg["integrations"][0], 60)]
    with devdash.integration_state_files(st):
        path = st.integrations[0].state_file
        assert path is not None
        assert os.stat(os.path.dirname(path)).st_mode & 0o777 == 0o700
        assert os.stat(path).st_mode & 0o777 == 0o600
        assert open(path, "rb").read() == b""
    assert st.integrations[0].state_file is None
    assert not os.path.exists(path)


def test_integration_action_queue_drops_presses_after_eight():
    ig = integration("pass", keys={"j": "newer"})
    actions = ["newer", "older"] * 4
    assert all(ig.queue_action(action) for action in actions)
    assert not ig.queue_action("scope")
    assert list(ig.actions) == actions


def test_integration_action_queue_is_fifo(tmp_path):
    log = tmp_path / "actions.txt"
    code = ("import os; "
            "open(os.environ['ACTION_LOG'], 'a').write("
            "os.environ.get('DEVDASH_ACTION', 'interval') + '\\n')")
    ig = integration(code, env={"ACTION_LOG": str(log)}, keys={"j": "newer"})
    st = devdash.State()
    st.width = 40
    thread = devdash.threading.Thread(target=ig.loop, args=(st,), daemon=True)
    thread.start()

    def wait_for_count(count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            lines = log.read_text().splitlines() if log.exists() else []
            if len(lines) >= count:
                return lines
            time.sleep(0.01)
        pytest.fail(f"only {len(lines)} integration runs completed")

    try:
        wait_for_count(1)
        actions = ["newer", "older"] * 4
        assert all(ig.queue_action(action) for action in actions)
        assert wait_for_count(9)[:9] == ["interval", *actions]
        time.sleep(0.05)
        assert log.read_text().splitlines() == ["interval", *actions]
    finally:
        ig.stop()
        thread.join(5)
    assert not thread.is_alive()


def test_focus_cycles_in_screen_order_and_dispatches_only_to_focused_binding():
    bottom = integration("pass", name="bottom", position="bottom", keys={"j": "older"})
    top = integration("pass", name="top", position="top", keys={"k": "newer"})
    st = devdash.State()
    st.keys = {"refresh": "r", "quit": "q", "focus": "tab"}
    st.integrations = [bottom, top]  # config order differs from screen order
    focusable = devdash.focusable_integrations(st)
    assert focusable == [top, bottom]
    st.focus = focusable[0]
    devdash.dispatch_key(st, "k")
    assert list(top.actions) == ["newer"] and not bottom.actions
    devdash.dispatch_key(st, "tab")
    assert st.focus is bottom
    devdash.dispatch_key(st, "j")
    assert list(bottom.actions) == ["older"]
    devdash.dispatch_key(st, "tab")
    assert st.focus is top


def test_each_section_lists_its_own_keys_and_footer_holds_global_keys():
    top = integration("pass", name="top", position="top", keys={"j": "newer", "k": "older"})
    bottom = integration("pass", name="bottom", position="bottom", keys={"x": "other"})
    st = devdash.State()
    st.show_usage = False
    st.keys = {"refresh": "R", "quit": "q", "focus": "tab"}
    st.integrations = [bottom, top]
    st.focus = top
    console = Console(record=True, width=120)
    console.print(devdash.Dashboard(st), height=10_000)
    lines = [row.rstrip() for row in console.export_text().splitlines()]
    assert lines[0] == "▸ TOP"
    assert lines[1] == "j newer · k older"
    review = next(i for i, row in enumerate(lines) if row.startswith("REVIEW REQUESTED"))
    assert "R refresh" in lines[review:lines.index("BOTTOM")]
    assert lines.count("R refresh") == 1
    assert lines[lines.index("BOTTOM") + 1] == "x other"
    assert lines[-1] == "q quit · tab focus"


def test_refresh_hint_follows_my_prs_when_review_is_hidden_and_focus_needs_two():
    ig = integration("pass", keys={"j": "newer"})
    st = devdash.State()
    st.show_usage = st.show_review = False
    st.integrations = [ig]
    st.focus = ig
    console = Console(record=True, width=80)
    console.print(devdash.Dashboard(st), height=10_000)
    lines = [row.rstrip() for row in console.export_text().splitlines()]
    assert lines.index("r refresh") < lines.index("▸ T")
    assert lines[-1] == "q quit"


def test_key_hint_rows_truncate_with_ellipsis():
    ig = integration("pass", keys={"j": "long_action_name", "k": "another_long_name"})
    st = devdash.State()
    st.show_usage = st.show_review = False
    st.integrations = [ig]
    console = Console(record=True, width=24)
    console.print(devdash.Dashboard(st), height=10_000)
    hint = console.export_text().splitlines()[-3]
    assert hint.startswith("j long_action_name") and hint.rstrip().endswith("…")
    assert len(hint) <= 24


def test_section_headings_show_fetch_state_and_countdown_but_once_has_none():
    now, mono = 1_800_000_000, 1_000.0
    st = devdash.State()
    st.mine = []
    st.prs_at = now - 12
    st.global_next_fetch = mono + 48
    prs = devdash.render_mine(st, 80, now, mono)[0].plain
    assert "fetched 12s ago · next fetch 48s" in prs
    assert "next fetch 47s" in devdash.section_status(
        now - 12, False, mono + 48, now, mono + 1)[0]

    st.usage_at = now - 12
    usage = devdash.render_usage(st, 80, now, mono)[0].plain
    assert "fetched 12s ago · next fetch 48s" in usage
    st.prs_fetching = st.usage_fetching = True
    assert "fetching…" in devdash.render_mine(st, 80, now, mono)[0].plain
    assert "fetching…" in devdash.render_review(st, 80, now, mono)[0].plain
    assert "fetching…" in devdash.render_usage(st, 80, now, mono)[0].plain

    ig = integration("pass")
    ig.result = ([], now - 12, None)
    ig.next_run_at = mono + 48
    assert "fetched 12s ago · next fetch 48s" in devdash.render_integration(
        ig, now, monotonic_now=mono)[0].plain
    ig.running = True
    assert "fetching…" in devdash.render_integration(ig, now, monotonic_now=mono)[0].plain

    st.prs_fetching = st.usage_fetching = False
    st.global_next_fetch = None
    assert "next fetch" not in devdash.render_mine(st, 80, now, mono)[0].plain
    assert devdash.render_review(st, 80, now, mono)[0].plain == "REVIEW REQUESTED 0  fetched 12s ago"
    assert "next fetch" not in devdash.render_usage(st, 80, now, mono)[0].plain
    ig.running = False
    ig.next_run_at = None
    assert "next fetch" not in devdash.render_integration(ig, now, monotonic_now=mono)[0].plain


def test_integration_keeps_colour_and_links_drops_screen_control_and_gets_its_room():
    ig = integration(
        "import os; print('\\x1b[2J\\x1b[H\\x1b[1mbold\\x1b[0m '"
        " + '\\x1b]8;;https://x.test\\x1b\\\\link\\x1b]8;;\\x1b\\\\'"
        " + ' ' + os.environ['COLUMNS'] + 'x' + os.environ['LINES'] + ' ' + os.environ['GREETING'])",
        max_rows=7, env={"GREETING": "hi"})
    ig.poll(42)
    [row], at, err = ig.result
    assert err is None and at
    assert row.plain == "bold link 42x7 hi"
    assert {(row.plain[s.start:s.end], str(s.style)) for s in row.spans} >= {
        ("bold", "bold"), ("link", "link https://x.test")}


def test_integration_output_cannot_smuggle_terminal_control():
    hostile = ("\x1b[31mred\x1b[0m \x01\x1b\x9b2J \x1b]8;;https://x.test/\x9b2J\x1b\\bad link\x1b]8;;\x1b\\"
               " \x1b]8;;\x07\x1b[2J\x1b\\gone\r\n")
    out = io.StringIO()
    console = Console(file=out, force_terminal=True, color_system="truecolor", width=80,
                      _environ={"TERM": "xterm-256color"})
    for row in devdash.output_rows(hostile, 5):
        console.print(row)
    emitted = out.getvalue()
    assert "\x1b[31m" in emitted  # colour survives
    for bad in ("\x01", "\x9b", "\x07", "\x1b[2J", "\x1b]8;;https://x.test/"):
        assert bad not in emitted
    assert "bad link" in emitted


def test_integration_error_line_is_plain_text():
    ig = integration("import sys; sys.stderr.write('\\x1b[2J\\x1b[31mbad\\x07\\n'); sys.exit(2)")
    ig.poll(40)
    assert ig.result[2] == "exit 2: bad"


def test_integration_failure_keeps_the_last_good_rows_under_the_error():
    ig = integration("print('first')")
    ig.poll(40)
    ig.command = [sys.executable, "-c", "import sys; sys.exit('boom')"]
    ig.poll(40)
    _, at, err = ig.result
    assert err == "exit 1: boom"
    assert [r.plain for r in devdash.render_integration(ig, at)] == ["T  fetched 0s ago", "⚠ exit 1: boom", "first"]


def test_integration_timeout_kills_the_whole_process_group(tmp_path):
    pid_file = tmp_path / "child.pid"
    ig = integration(spawner(pid_file), timeout=3)
    ig.poll(40)
    assert ig.result[2] == "timed out after 3s"
    assert gone(wait_for_pid(pid_file)), "the command's child outlived the timeout"


def test_integration_unexpected_capture_error_still_kills_the_command(tmp_path, monkeypatch):
    pid_file = tmp_path / "child.pid"

    def broken(p, deadline):
        wait_for_pid(pid_file)
        raise OSError("selector failed")

    monkeypatch.setattr(devdash, "capture", broken)
    ig = integration(spawner(pid_file), timeout=60)
    ig.poll(40)
    assert ig.result[2] == "selector failed"
    assert gone(int(pid_file.read_text())), "the command's child outlived the error"


def test_integration_output_is_cut_to_max_rows_with_a_count():
    rows = devdash.output_rows("".join(f"row {i}\n" for i in range(10)) + "\n\n", 4)
    assert [r.plain for r in rows] == ["row 0", "row 1", "row 2", "… 7 more rows"]


def test_integration_flooding_stdout_is_stopped_at_the_output_cap():
    flood = [sys.executable, "-c", "import sys\nwhile True: sys.stdout.buffer.write(b'x' * 65536)"]
    p = devdash.subprocess.Popen(flood, stdout=devdash.subprocess.PIPE, stderr=devdash.subprocess.PIPE,
                                 start_new_session=True)
    started = time.monotonic()
    out, err, full = devdash.capture(p, started + 30)
    assert full and len(out) == devdash.MAX_OUTPUT and err == b""
    assert p.returncode is not None and time.monotonic() - started < 10  # stopped, not left to time out
    ig = integration("import sys\nwhile True: sys.stdout.write('row\\n' * 4096)", timeout=30, max_rows=3)
    ig.poll(40)
    rows, _, err = ig.result
    assert err is None and [r.plain for r in rows][:2] == ["row", "row"]


def test_integration_keeps_only_the_tail_of_a_flooded_stderr():
    ig = integration("import sys\nfor _ in range(200): sys.stderr.write('noise ' * 10000 + '\\n')\n"
                     "sys.exit('the real reason')", timeout=30)
    p = devdash.subprocess.Popen(ig.command, stdout=devdash.subprocess.PIPE, stderr=devdash.subprocess.PIPE)
    _, err, _ = devdash.capture(p, time.monotonic() + 30)
    assert len(err) <= devdash.ERR_TAIL and err.endswith(b"the real reason\n")
    ig.poll(40)
    assert ig.result[2] == "exit 1: the real reason"


def test_stopping_an_integration_kills_its_running_command_and_children(tmp_path):
    pid_file = tmp_path / "child.pid"
    ig = integration(spawner(pid_file), timeout=60)
    thread = devdash.threading.Thread(target=ig.loop, args=(devdash.State(),), daemon=True)
    thread.start()
    try:
        child = wait_for_pid(pid_file)
    finally:
        ig.stop()
    thread.join(5)
    assert not thread.is_alive()  # the loop ends instead of waiting out the 60s timeout
    assert gone(child), "the command's child outlived devdash"


def test_integrations_render_in_their_slots_between_built_in_sections():
    st = devdash.State()
    st.integrations = []
    st.show_usage = st.show_review = True
    st.usage, st.mine, st.review = {"reports": []}, [], []
    for name in ("bottom", "after-my-prs", "top", "after-usage", "bottom2"):
        ig = integration("", name=name, position=name.rstrip("2"))
        ig.result = ([devdash.line(f"{name} body")], 0, None)
        st.integrations.append(ig)
    console = Console(record=True, width=50)
    console.print(devdash.Dashboard(st), height=10_000)
    heads = [r for r in console.export_text().splitlines() if r.split(" ")[0].isupper() and r.strip()]
    assert [h.split("  ")[0].strip() for h in heads] == [
        "TOP", "USAGE", "AFTER-USAGE", "MY PRS 0", "AFTER-MY-PRS", "REVIEW REQUESTED 0", "BOTTOM", "BOTTOM2"]
