"""ipedro/merge_policy.py — which /evolve PRs may merge without the owner.

This file is itself guarded by the policy, so nothing here can be loosened
in a PR that merges on its own.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ipedro import merge_policy
from ipedro.merge_policy import CONTENT, GUARDED, decide

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "dale-evolve.yml"


# ── the rule ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("files", [
    ["ipedro/prompts.py"],
    ["ipedro/handlers/automod.py", "tests/test_automod.py"],
    ["ipedro/dale_gif_seeds.py", "tests/test_dale_gifs.py"],
    ["ipedro/personas.py", "tests/test_new_persona_bits.py"],
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
    "ipedro/dale_gifs.py",
    "requirements.txt",
    "requirements-dev.txt",
    "docker/Dockerfile",
    "tests/conftest.py",
    "tests/test_merge_policy.py",
    "tests/test_evolve.py",
    "tests/test_automod_rules.py",
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
    auto, _ = decide(["ipedro/evolve.py", "ipedro/prompts_extra.py"])
    assert auto is False


# ── the lists stay honest ────────────────────────────────────────────────────

def test_no_content_pattern_reaches_guarded_code():
    """Guarded wins anyway, but a CONTENT pattern that matched guarded
    source would be a misunderstanding waiting to become a hole. The one
    deliberate overlap is tests: `tests/test_*.py` is content while a few
    test files are guarded — precedence handles that, and
    test_guarded_files_never_merge_on_their_own proves it per file."""
    from fnmatch import fnmatchcase
    literal_guarded = [
        p for p in GUARDED
        if not any(c in p for c in "*?[") and not p.startswith("tests/")
    ]
    for path in literal_guarded:
        assert not any(fnmatchcase(path, c) for c in CONTENT), path


def test_every_named_file_still_exists():
    """A rename would silently un-guard (or un-list) a file."""
    for p in GUARDED + CONTENT:
        if any(c in p for c in "*?["):
            continue
        assert (ROOT / p).exists(), f"{p} is named in merge_policy but gone"


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
    assert imported <= {"__future__", "sys", "fnmatch"}, imported


@pytest.mark.parametrize("stdin,code,word", [
    ("ipedro/prompts.py\ntests/test_x.py\n", 0, "content only"),
    ("ipedro/evolve.py\n", 1, "guarded"),
    ("", 1, "no changed files"),
])
def test_the_command_line_contract(tmp_path, stdin, code, word):
    """Exactly how the workflow calls it: a copy outside the repo, paths on
    stdin, exit status as the verdict."""
    copy = tmp_path / "merge_policy.py"
    copy.write_text(Path(merge_policy.__file__).read_text())
    out = subprocess.run(
        [sys.executable, str(copy)], input=stdin,
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert out.returncode == code, out.stderr
    assert word in out.stdout


# ── the workflow keeps the promises the policy depends on ────────────────────

def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["implement"]


def _step(name):
    return next(s for s in _workflow()["steps"] if s.get("name") == name)


def test_the_workflow_consults_mains_policy_not_the_branchs():
    script = _step("Open the PR, judge it, merge or leave it")["run"]
    assert "git show origin/main:ipedro/merge_policy.py" in script
    assert "python ipedro/merge_policy.py" not in script


def test_nothing_merges_unless_the_workflows_own_tests_passed():
    script = _step("Open the PR, judge it, merge or leave it")["run"]
    gate = script.index('steps.verify.outputs.passed }}" != "true"')
    assert gate < script.index("gh pr merge")


def test_the_agent_never_holds_a_github_token():
    job = _workflow()
    assert "GH_TOKEN" not in (job.get("env") or {})
    checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False
    agent = _step("Implement the request")
    assert "GH_TOKEN" not in (agent.get("env") or {})
    args = agent["with"]["claude_args"]
    assert "Bash(gh" not in args            # no GitHub CLI at all
    assert "git push" not in args           # pushing is a later step's job
    assert "WebFetch" in args.split("--disallowedTools", 1)[1]
