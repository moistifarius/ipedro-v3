"""The files a deployment is built from agree with each other and the docs.

Found by the audit: every doc told you to `cp .env.example .env` and the file
did not exist, and `docker compose up` run from docker/ never applied the
POSTGRES_* / PGDATA_HOST_PATH settings in the .env it was told about, because
`env_file:` feeds the containers while ${...} substitution reads a different
file. None of that fails loudly, so these pin it.
"""

from __future__ import annotations

import re
from pathlib import Path

from ipedro.config import Settings

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
COMPOSE = ROOT / "docker" / "docker-compose.yml"

_ASSIGNMENT = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=(.*)$")


def _keys(*, commented: bool) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in EXAMPLE.read_text().splitlines():
        m = _ASSIGNMENT.match(line)
        if not m:
            continue
        if line.lstrip().startswith("#") and not commented:
            continue
        out[m.group(1)] = m.group(2)
    return out


def test_the_example_exists_and_is_plain_key_value_lines():
    assert EXAMPLE.is_file()
    for n, line in enumerate(EXAMPLE.read_text().splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert _ASSIGNMENT.match(line), f".env.example:{n}: not KEY=value: {line!r}"


def test_every_setting_is_listed():
    """DEPLOY.md says the full list of tunables is in .env.example."""
    listed = set(_keys(commented=True))
    missing = [f.upper() for f in Settings.model_fields if f.upper() not in listed]
    assert not missing, f"settings missing from .env.example: {missing}"


def test_nothing_listed_is_a_setting_that_does_not_exist():
    """A typo'd key is ignored (extra='ignore'), so it would silently do nothing."""
    from_config = {f.upper() for f in Settings.model_fields}
    compose_only = {"POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "PGDATA_HOST_PATH"}
    other_readers = {"RADIO_FX_LIVE_URLS"}              # read from os.environ directly
    unknown = set(_keys(commented=True)) - from_config - compose_only - other_readers
    assert not unknown, f"keys in .env.example that nothing reads: {sorted(unknown)}"


def test_every_compose_substitution_is_documented():
    text = COMPOSE.read_text()
    # `$$` is Compose's escape: that one is left for the container's shell.
    used = set(re.findall(r"(?<!\$)\$\{([A-Z][A-Z0-9_]*)", text))
    assert used, "expected ${...} substitutions in the compose file"
    missing = used - set(_keys(commented=True))
    assert not missing, f"compose substitutes {sorted(missing)}, .env.example never mentions them"


def test_the_example_ships_no_secrets():
    secretish = re.compile(
        r"sk-[A-Za-z0-9]|\d{6,}:[A-Za-z0-9_-]{20,}|ghp_|github_pat_|AIza", re.IGNORECASE)
    for key, value in _keys(commented=True).items():
        assert not secretish.search(value), f"{key} looks like a real credential"
    uncommented = _keys(commented=False)
    for required in ("TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                     "EVOLVE_GITHUB_TOKEN", "REDDIT_CLIENT_SECRET"):
        assert uncommented[required] == "", f"{required} must be blank in the example"


def test_the_example_loads_as_settings(monkeypatch):
    for key in _keys(commented=True):
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=EXAMPLE)              # type: ignore[call-arg]
    assert s.database_url.startswith("postgresql://")
    assert s.bot_name == "Dale"                  # nothing in the example moves a default


def test_the_compose_file_says_how_to_run_it():
    """The header must name the flag that makes ${...} read the .env."""
    header = COMPOSE.read_text().split("services:")[0]
    assert "--env-file ../.env" in header


def test_docs_that_start_the_stack_pass_the_flag():
    """`env_file:` alone does not feed ${POSTGRES_PASSWORD} / ${PGDATA_HOST_PATH}."""
    starting = re.compile(r"docker compose(?!\s+--env-file\s+\.\./\.env)\s+(up|build)\b")
    offenders = []
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")),
                 ROOT / "scripts" / "migrate_pgdata_to_appdata.sh"]:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if starting.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not offenders, "start the stack with --env-file ../.env:\n" + "\n".join(offenders)


def test_ci_runs_the_tests_on_the_python_production_runs():
    """The suite used to run on 3.11 in CI while the image ships 3.12."""
    import yaml

    dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
    prod = re.search(r"^FROM python:(\d+\.\d+)", dockerfile, re.MULTILINE).group(1)
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "dale-evolve.yml").read_text())
    versions = [
        str(step["with"]["python-version"])
        for job in workflow["jobs"].values() for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-python")
    ]
    assert versions and set(versions) == {prod}, (versions, prod)


def test_relative_links_in_the_docs_point_at_files_that_exist():
    """The audit found docs that sent you to files that weren't there."""
    docs = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    link = re.compile(r"\]\((?!https?://|mailto:|#)([^)#\s]+)(?:#[^)]*)?\)")
    broken = []
    for doc in docs:
        for target in link.findall(doc.read_text()):
            if not (doc.parent / target).exists():
                broken.append(f"{doc.relative_to(ROOT)} -> {target}")
    assert not broken, "\n".join(broken)
