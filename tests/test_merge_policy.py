"""ipedro/merge_policy.py — which /evolve PRs may merge without the owner —
and the workflow that applies it.

This file is itself guarded by the policy, so nothing here can be loosened
in a PR that merges on its own.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ipedro import evolve, merge_policy
from ipedro.merge_policy import (
    CONTENT, CONTENT_TESTS, DATA_MODULES, GUARDED, NEW_TEST, data_violation, decide,
)

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "dale-evolve.yml"


def raw(status: str, path: str, old: str = "100644", new: str | None = None) -> str:
    """One line of `git diff --raw --no-renames`, which is what the workflow
    feeds the policy."""
    if new is None:
        new = "000000" if status == "D" else "100644"
    if status == "A":
        old = "000000"
    return f":{old} {new} 1111111 2222222 {status}\t{path}"


# ── the rule ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("files", [
    ["ipedro/prompts.py"],
    ["ipedro/handlers/automod_bits.py", "tests/test_automod.py"],
    ["ipedro/dale_gif_seeds.py", raw("A", "tests/test_dale_gif_extras.py")],
    ["ipedro/prompts.py", raw("A", "tests/test_new_prompt_bits.py")],
])
def test_content_only_changes_may_merge(files):
    auto, reason = decide(files)
    assert auto is True, reason


@pytest.mark.parametrize("path", [
    ".github/workflows/dale-evolve.yml",
    "ipedro/merge_policy.py",
    "ipedro/evolve.py",
    "ipedro/handlers/evolve.py",
    "ipedro/auth.py",
    "ipedro/config.py",
    "ipedro/handlers/common.py",
    "ipedro/introspection.py",
    "ipedro/capabilities.py",
    "ipedro/addressed.py",
    "ipedro/handlers/chat.py",
    "ipedro/memory/store.py",
    "ipedro/memory/context_builder.py",
    "ipedro/openai_client.py",
    "ipedro/db/schema.sql",
    "ipedro/automod_media.py",
    "ipedro/automod_types.py",
    "ipedro/handlers/automod.py",         # the part that DECIDES; the bits are data
    "ipedro/personas.py",                 # the part that chooses; the words are prompts.py
    "ipedro/dale_gifs.py",
    "ipedro/net_safety.py",
    "ipedro/ratelimit.py",
    "requirements.txt",
    "requirements-dev.txt",
    "docker/Dockerfile",
    ".env.example",
    "pytest.ini",
    "tests/conftest.py",
    "tests/test_merge_policy.py",
    "tests/test_evolve.py",
    "tests/test_automod_rules.py",
    "tests/test_hub.py",
    "tests/test_identity.py",
    "tests/test_config_auth.py",
    "tests/test_capabilities.py",
])
def test_guarded_files_never_merge_on_their_own(path):
    auto, reason = decide([path])
    assert auto is False and "guarded" in reason


def test_one_guarded_file_holds_back_an_otherwise_content_pr():
    auto, reason = decide(["ipedro/prompts.py", "ipedro/evolve.py"])
    assert auto is False
    assert "ipedro/evolve.py" in reason


@pytest.mark.parametrize("files", [
    ["ipedro/brand_new_module.py"],                  # unknown: fails closed
    ["ipedro/handlers/utility.py"],                  # real, but not content
    ["ipedro/prompts.py", "ipedro/handlers/duckhunt.py"],
    ["README.md"],
    ["docs/COMMANDS.md"],
])
def test_anything_not_listed_as_content_waits(files):
    auto, _ = decide(files)
    assert auto is False


def test_nothing_to_judge_is_not_a_yes():
    assert decide([])[0] is False
    assert decide(["", "  "])[0] is False


def test_a_renamed_guarded_file_is_caught_by_its_old_name():
    """The workflow diffs with --no-renames, so a move shows up as the old
    path deleted plus the new one added — the old one is guarded."""
    auto, reason = decide([raw("D", "ipedro/evolve.py"), raw("A", "ipedro/prompts_extra.py")])
    assert auto is False and "guarded" in reason


# ── tests are code: they get less trust than data ────────────────────────────

@pytest.mark.parametrize("path", [
    "tests/test_x/conftest.py",           # fnmatch's `*` once let these through
    "tests/test_helpers/helper.py",
    "tests/test_x/sub/conftest.py",
    "tests/test_x/__init__.py",
    "tests/conftest.py",
    "tests/helpers/conftest.py",
    "tests/sub/test_thing.py",
    "tests/__init__.py",
    "tests/test_x.py/../../ipedro/auth.py",
])
def test_only_a_flat_test_file_counts_as_a_test(path):
    assert NEW_TEST.fullmatch(path) is None
    assert decide([raw("A", path)])[0] is False


def test_a_conftest_is_guarded_at_any_depth():
    for path in ("tests/conftest.py", "tests/test_x/conftest.py", "conftest.py",
                 "ipedro/conftest.py", "tests/a/b/c/conftest.py"):
        auto, reason = decide([raw("A", path)])
        assert not auto and "guarded" in reason, path


def test_a_new_test_file_may_ride_along_with_content():
    auto, reason = decide(["ipedro/prompts.py", raw("A", "tests/test_the_new_thing.py")])
    assert auto, reason


def test_editing_an_existing_test_that_pins_more_than_content_waits():
    """The pins live in the unguarded tests too (what the plain bot may say,
    how deep the hub goes). Weakening one is how a content PR would get a
    code change past a green suite."""
    for existing in ("tests/test_cost_report.py", "tests/test_karma.py"):
        auto, reason = decide([raw("M", existing)])
        assert not auto and "existing tests" in reason, reason


def test_the_tests_that_pin_only_content_may_be_edited():
    for existing in CONTENT_TESTS:
        assert decide([raw("M", existing)])[0], existing


def test_deleting_anything_waits():
    for path in ("tests/test_something.py", "ipedro/prompts.py", "ipedro/dale_gif_seeds.py"):
        auto, reason = decide([raw("D", path)])
        assert not auto and "deletes" in reason, (path, reason)


@pytest.mark.parametrize("old,new,status", [
    ("100644", "120000", "T"),            # became a symlink
    ("100644", "160000", "T"),            # became a submodule
    ("100644", "100755", "M"),            # gained the executable bit
    ("000000", "120000", "A"),            # a new symlink
    ("100755", "100644", "M"),            # lost it
])
def test_only_ordinary_files_qualify(old, new, status):
    auto, reason = decide([f":{old} {new} 1 2 {status}\tipedro/prompts.py"])
    assert not auto and "ordinary files" in reason


# ── "content" is checked, not assumed ────────────────────────────────────────

@pytest.mark.parametrize("source", [
    "",
    '"""just a docstring"""\n',
    "X = 1\nY = 'two'\nZ = ('a', 'b')\nW = {'k': ['v', 3, None, True]}\n",
    "from __future__ import annotations\n\nA: tuple[str, ...] = ('a',)\n",
    "import re\nP = re.compile(r'\\bgays?\\b', re.IGNORECASE)\nQ = re.compile('x', re.I | re.M)\n",
    "from ipedro.automod_types import DaleGif, MediaResponse\n"
    "M = MediaResponse('gif', 'https://x/y.gif', 'cap', fallback='fb')\n"
    "D = DaleGif('tag', caption='c')\nT = ((D, M), 'x' + 'y')\n",
    "A = B = 'same'\nC = A\n",
])
def test_data_is_data(source):
    assert data_violation(source) is None


@pytest.mark.parametrize("source,why", [
    ("import os\n", "imports"),
    ("from os import path\n", "imports"),
    ("import re as r\n", "imports"),
    ("from . import x\n", "imports"),
    ("from ipedro.automod_types import Other\n", "imports"),
    ("import httpx\nX = httpx.get('http://x')\n", "imports"),
    ("X = open('/etc/passwd').read()\n", "call"),
    ("X = __import__('os')\n", "call"),
    ("import re\nX = re.sub('a', 'b', 'c')\n", "re.sub"),
    ("import re\nX = re.compile(*['a'])\n", "`*`"),
    ("import re\nX = re.compile(**{'pattern': 'a'})\n", "`**`"),
    ("def f():\n    return 1\n", "FunctionDef"),
    ("class C:\n    x = 1\n", "ClassDef"),
    ("async def f():\n    pass\n", "AsyncFunctionDef"),
    ("if True:\n    X = 1\n", "If"),
    ("for i in range(3):\n    X = i\n", "For"),
    ("with open('x') as f:\n    pass\n", "With"),
    ("try:\n    X = 1\nexcept Exception:\n    pass\n", "Try"),
    ("X = 1\nX += 1\n", "AugAssign"),
    ("print('hi')\n", "expression statement"),
    ("X = f'{1}'\n", "JoinedStr"),
    ("X = [i for i in range(3)]\n", "ListComp"),
    ("X = lambda: 1\n", "Lambda"),
    ("X = (1, 2)[0]\n", "Subscript"),
    ("X = ().__class__\n", "Attribute"),
    ("X = {**{'a': 1}}\n", "`**`"),
    ("X = (y := 1)\n", "NamedExpr"),
    ("X.y = 1\n", "plain name"),
    ("X[0] = 1\n", "plain name"),
    ("global X\nX = 1\n", "Global"),
    ("del X\n", "Delete"),
    ("assert True\n", "Assert"),
    ("x = (\n", "doesn't parse"),
])
def test_anything_else_is_not_data(source, why):
    bad = data_violation(source)
    assert bad is not None and why in bad, bad


def test_the_content_modules_in_the_repo_are_data_today():
    """The docstrings have always said these were data only. Now it's true,
    and this is what keeps it true: a PR that breaks it fails here as well
    as in the policy."""
    for rel in DATA_MODULES:
        assert data_violation((ROOT / rel).read_text()) is None, rel


def _checkout(root: Path, prompts: str) -> Path:
    (root / "ipedro").mkdir(parents=True)
    (root / "ipedro" / "prompts.py").write_text(prompts)
    return root


def test_the_policy_reads_the_prs_copy_of_a_data_module(tmp_path):
    good = _checkout(tmp_path / "good", "X = 'fine'\n")
    bad = _checkout(tmp_path / "bad", "import os\nX = os.environ['OPENAI_API_KEY']\n")

    assert decide(["ipedro/prompts.py"], root=good)[0] is True
    auto, reason = decide(["ipedro/prompts.py"], root=bad)
    assert not auto and "no longer data only" in reason and "imports" in reason


def test_a_data_module_the_policy_cannot_read_waits(tmp_path):
    auto, reason = decide(["ipedro/prompts.py"], root=tmp_path)
    assert not auto and "can't read" in reason


def test_without_a_checkout_the_policy_judges_by_path_alone(tmp_path):
    assert decide(["ipedro/prompts.py"])[0] is True


def test_a_test_file_is_not_parsed_as_data(tmp_path):
    """Tests are code; they're held to the path rules, not the data rules."""
    assert decide([raw("A", "tests/test_x.py")], root=tmp_path)[0] is True


# ── the lists stay honest ────────────────────────────────────────────────────

def test_no_content_path_is_guarded():
    from fnmatch import fnmatchcase
    for path in CONTENT:
        assert not any(fnmatchcase(path, g) for g in GUARDED), path


def test_the_data_modules_are_content_and_nothing_else_is_unlisted():
    assert set(DATA_MODULES) <= set(CONTENT)
    assert set(CONTENT_TESTS) <= set(CONTENT)
    assert set(CONTENT) == set(DATA_MODULES) | set(CONTENT_TESTS)


def test_every_named_file_still_exists():
    """A rename would silently un-guard (or un-list) a file."""
    for p in GUARDED + CONTENT:
        if any(c in p for c in "*?["):
            continue
        assert (ROOT / p).exists(), f"{p} is named in merge_policy but gone"


def test_every_test_file_that_pins_a_guard_is_not_content():
    """A test of the auth rules, the hub depth cap, the plain bot's voice...
    must never become editable by a content PR."""
    for name in ("test_auth.py", "test_hub.py", "test_identity.py",
                 "test_config_auth.py", "test_net_safety.py", "test_ratelimit.py"):
        assert f"tests/{name}" not in CONTENT


def test_the_policy_runs_detached_from_the_package():
    """The workflow runs main's copy as a lone file, so it can't import
    anything from ipedro — only the standard library."""
    tree = ast.parse(Path(merge_policy.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


def _run_cli(tmp_path: Path, stdin: str, *args: str, cwd: Path | None = None):
    """Exactly how the workflow calls it: a copy outside the repo, git's own
    lines on stdin, exit status as the verdict."""
    copy = tmp_path / "merge_policy.py"
    copy.write_text(Path(merge_policy.__file__).read_text())
    return subprocess.run(
        [sys.executable, str(copy), *args], input=stdin,
        capture_output=True, text=True, cwd=cwd or tmp_path,
    )


@pytest.mark.parametrize("stdin,code,word", [
    (f"{raw('M', 'ipedro/prompts.py')}\n{raw('A', 'tests/test_x.py')}\n", 0, "content only"),
    ("ipedro/prompts.py\n", 0, "content only"),
    ("ipedro/evolve.py\n", 1, "guarded"),
    ("", 1, "no changed files"),
])
def test_the_command_line_contract(tmp_path, stdin, code, word):
    repo = tmp_path / "pr"
    (repo / "ipedro").mkdir(parents=True)
    (repo / "ipedro" / "prompts.py").write_text("X = 'fine'\n")
    out = _run_cli(tmp_path, stdin, "--root", str(repo))
    assert out.returncode == code, out.stderr
    assert word in out.stdout


def test_the_command_line_defaults_to_the_current_directory(tmp_path):
    repo = tmp_path / "pr"
    (repo / "ipedro").mkdir(parents=True)
    (repo / "ipedro" / "prompts.py").write_text("import os\n")
    out = _run_cli(tmp_path, "ipedro/prompts.py\n", cwd=repo)
    assert out.returncode == 1 and "no longer data only" in out.stdout


def test_a_bad_command_line_is_not_a_yes(tmp_path):
    out = _run_cli(tmp_path, "ipedro/prompts.py\n", "--root")
    assert out.returncode == 2


# ── the workflow keeps the promises the policy depends on ────────────────────

def _jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _job(name):
    return _jobs()[name]


def _step(job, name):
    return next(s for s in _job(job)["steps"] if s.get("name") == name)


def _text(node) -> str:
    return yaml.safe_dump(node)


def test_the_work_is_split_across_three_jobs_by_what_they_may_touch():
    assert set(_jobs()) == {"implement", "verify", "publish"}
    # the two that run the agent's code can only read
    assert _job("implement")["permissions"] == {"contents": "read"}
    assert _job("verify")["permissions"] == {"contents": "read"}
    assert _job("publish")["permissions"] == {
        "contents": "write", "issues": "write", "pull-requests": "write",
    }


def test_no_job_that_runs_the_agents_code_can_write_to_github():
    for name in ("implement", "verify"):
        text = _text(_job(name))
        for forbidden in ("github.token", "GITHUB_TOKEN", "GH_TOKEN", "x-access-token",
                          "gh pr", "gh issue", "git push"):
            assert forbidden not in text, (name, forbidden)
        checkout = next(s for s in _job(name)["steps"]
                        if s.get("uses", "").startswith("actions/checkout"))
        assert checkout["with"]["persist-credentials"] is False


def test_the_api_key_reaches_only_the_agent_step():
    secrets = {name: re.findall(r"secrets\.[A-Za-z_]+", _text(job)) for name, job in _jobs().items()}
    assert secrets == {"implement": ["secrets.ANTHROPIC_API_KEY"], "verify": [], "publish": []}
    agent = _step("implement", "Implement the request")
    assert set(agent["env"]) == {
        "ANTHROPIC_API_KEY", "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", "ISSUE_TITLE", "ISSUE_BODY",
    }
    assert "env" not in _job("implement") or "ANTHROPIC" not in _text(_job("implement")["env"])


def test_the_agent_is_boxed_in():
    agent = _step("implement", "Implement the request")
    run = agent["run"]
    assert "--restricted" in run                 # no project settings, files confined
    assert "--permission-mode dontAsk" in run    # what isn't listed is refused
    assert "--max-turns" in run and "--max-budget-usd" in run
    allowed = run.split("--allowedTools", 1)[1].split("\\", 1)[0]
    for never in ("Bash(gh", "Bash(git push", "Bash(git checkout", "Bash(curl", "Bash(rm",
                  "Bash(python -c", "Bash(pip", "Bash(npm", "Bash(sh", "Bash(bash",
                  "WebFetch", "WebSearch"):
        assert never not in allowed, never
    assert "WebFetch" in run.split("--disallowedTools", 1)[1]
    assert not any(s.get("uses", "").startswith("anthropics/") for j in _jobs().values()
                   for s in j["steps"])         # no third-party action holds the token


def test_the_cli_is_pinned():
    install = _step("implement", "Install the Claude Code CLI")["run"]
    assert re.search(r"@anthropic-ai/claude-code@\d+\.\d+\.\d+\b", install), install


def test_only_githubs_own_actions_are_used():
    uses = [s["uses"] for j in _jobs().values() for s in j["steps"] if "uses" in s]
    assert uses and all(u.startswith("actions/") for u in uses), uses


def test_only_the_owners_marked_issues_start_a_run():
    cond = _job("implement")["if"]
    assert "github.event.sender.login == 'moistifarius'" in cond
    assert f"'{evolve.EVOLVE_MARKER}'" in cond


def test_issue_text_reaches_no_shell_script_in_any_job():
    """The classic Actions injection hole: ${{ github.event.issue.title }}
    pasted into a `run:`. Through `env:` it's only ever a variable."""
    for jname, job in _jobs().items():
        for step in job["steps"]:
            run = step.get("run", "")
            assert "github.event.issue.title" not in run, (jname, step.get("name"))
            assert "github.event.issue.body" not in run, (jname, step.get("name"))
            assert not re.search(r"\$\{\{\s*(needs|steps)\.[^}]*(summary|title|body)", run), (
                jname, step.get("name"))


def test_every_run_gets_its_own_branch():
    """A re-run used to push to the same branch, have the push rejected, and
    then judge and merge the EARLIER run's pull request."""
    for jname in ("implement", "publish"):
        branch = _job(jname)["env"]["BRANCH"]
        assert "github.run_id" in branch and "github.run_attempt" in branch
    assert _job("implement")["env"]["BRANCH"] == _job("publish")["env"]["BRANCH"]


def test_the_agents_commits_travel_as_data():
    collect = _step("implement", "Collect the work")["run"]
    assert "git bundle create" in collect
    assert any(s.get("uses", "").startswith("actions/upload-artifact")
               for s in _job("implement")["steps"])


def test_a_run_that_did_not_finish_cleanly_never_merges_on_its_own():
    """The agent crashing or running out of turns, or leaving work
    uncommitted that the workflow then swept into a commit, is whatever
    happened to be on disk — fine to show the owner, never to merge."""
    agent = _step("implement", "Implement the request")
    assert agent["continue-on-error"] is True
    collect = _step("implement", "Collect the work")
    assert collect["if"] == "always()"
    run = collect["run"]
    assert 'AGENT_OUTCOME" = "success" ] || clean=false' in run
    swept = run.split("git status --porcelain", 1)[1].split("git commit", 1)[0]
    assert "clean=false" in swept


def test_the_suite_must_pass_and_not_shrink():
    compare = _step("verify", "Compare")["run"]
    assert "head[2] == 0" in compare and "head[1] == 0" in compare      # exit status, failures
    assert "head[0] >= main[0]" in compare and "main[0] > 0" in compare  # no fewer passes than main
    for name in ("Run the suite on main", "Run the suite on the agent's commits"):
        assert "--junitxml" in _step("verify", name)["run"]


def test_publish_never_runs_the_agents_code():
    job = _job("publish")
    text = "\n".join(s.get("run", "") for s in job["steps"])
    for runs_code in ("pytest", "pip ", "npm ", "python -m", "import ", "make ", "./"):
        assert runs_code not in text, runs_code
    # the one interpreter call is main's policy, run as text over text
    pythons = re.findall(r"\bpython3?\b[^\n]*", text)
    assert len(pythons) == 1 and pythons[0].startswith('python3 "$RUNNER_TEMP/merge_policy.py"')
    checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout"))
    assert "ref" not in checkout.get("with", {})                 # this checkout IS main


def test_the_workflow_consults_mains_policy_not_the_branchs():
    script = _step("publish", "Judge it, merge or leave it")["run"]
    assert "git show origin/main:ipedro/merge_policy.py" in script
    assert "python3 ipedro/merge_policy.py" not in script
    assert "--root" in script and "git worktree add" in script   # the branch is only ever read


def test_nothing_merges_unless_every_condition_was_checked_first():
    script = _step("publish", "Judge it, merge or leave it")["run"]
    merge = script.index("gh pr merge")
    for check in ('"$TESTS_PASSED" = "true"', '"$CLEAN" = "true"', "headRefOid",
                  "merge_policy.py"):
        assert script.index(check) < merge, check
    # the merge itself names the commit, so GitHub refuses if the branch moved
    assert 'gh pr merge "$PR" --merge --match-head-commit "$HEAD"' in script


def test_what_is_merged_is_what_was_tested():
    """verify and publish both pin the commit the agent job reported."""
    for job in ("verify", "publish"):
        assert "needs.implement.outputs.head" in _text(_job(job))
    assert 'test "$(git rev-parse candidate)" = "$HEAD"' in _step(
        "verify", "Check out main beside the agent's commits")["run"]
    assert 'test "$(git rev-parse candidate)" = "$HEAD"' in _step(
        "publish", "Push the branch and open the pull request")["run"]


def test_a_failed_publish_is_reported_on_the_issue():
    last = _job("publish")["steps"][-1]
    assert last["if"] == "failure()" and "gh issue comment" in last["run"]
