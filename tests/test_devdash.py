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
