"""Which /evolve pull requests may merge without the owner looking.

The rule the owner approved: a PR auto-merges only when the workflow's own
test run passes AND every file it changes is plain content — prompts,
persona text, canned lines, the GIF table, ordinary tests. Anything else
waits for the owner. It's an allowlist, so it fails closed: a file this
list has never heard of (a new module, a moved one) waits too.

Two things make this a guardrail rather than a suggestion:

* The workflow runs MAIN's copy of this file, never the PR branch's — a
  PR that loosened these lists would otherwise get to approve itself.
  That's also why it imports nothing outside the standard library: it runs
  as a lone file pulled out of main, detached from the package.
* GUARDED is checked first and wins over CONTENT, and a test pins that no
  CONTENT pattern ever matches a GUARDED path, so widening CONTENT by
  mistake can't open up the self-modification machinery.

Content files hold data only. Anything that reaches the network or the DB,
or enforces a safety rule, lives in a reviewed file — which is why the GIF
table, the automod media fetcher and the safety-critical automod tests
were split out of the files around them.
"""

from __future__ import annotations

import sys
from fnmatch import fnmatchcase

# Plain content: changing these changes what the bot SAYS, not who it
# answers, what it remembers, what it can reach, or who may do what.
CONTENT: tuple[str, ...] = (
    "ipedro/prompts.py",
    "ipedro/personas.py",
    "ipedro/handlers/automod.py",
    "ipedro/dale_gif_seeds.py",
    "tests/test_*.py",
)

# Never auto-merged, even alongside content. Everything not in CONTENT
# already waits (fail-closed); these are named so the "never" is explicit
# and testable, not just a consequence of omission.
GUARDED: tuple[str, ...] = (
    # the self-modification machinery: edit any of it and the agent would
    # be rewriting its own guardrails
    ".github/*",
    "ipedro/merge_policy.py",
    "ipedro/evolve.py",
    "ipedro/handlers/evolve.py",
    # making other bots: new Telegram accounts, their tokens, their processes
    "ipedro/bots.py",
    "ipedro/supervisor.py",
    "ipedro/handlers/bots.py",
    "ipedro/identity.py",
    # who may do what, and what the bot can see about itself
    "ipedro/auth.py",
    "ipedro/config.py",
    "ipedro/handlers/common.py",
    "ipedro/introspection.py",
    "ipedro/capabilities.py",
    # the core: who it answers, what it remembers, how it talks to the model
    "ipedro/addressed.py",
    "ipedro/chat_policy.py",
    "ipedro/handlers/chat.py",
    "ipedro/memory/*",
    "ipedro/openai_client.py",
    "ipedro/db/*",
    # what content files may reach
    "ipedro/automod_media.py",
    "ipedro/dale_gifs.py",
    # dependencies and deployment: a supply-chain change is never "content"
    "requirements*.txt",
    "docker/*",
    # the tests that guard all of the above, and the harness they run in
    "tests/conftest.py",
    "tests/test_merge_policy.py",
    "tests/test_evolve.py",
    "tests/test_auth.py",
    "tests/test_common.py",
    "tests/test_introspection.py",
    "tests/test_automod_rules.py",
    "tests/test_bots.py",
)


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(path, p) for p in patterns)


def decide(changed: list[str]) -> tuple[bool, str]:
    """(may_auto_merge, reason). ``changed`` is every path the PR touches
    — renames split into delete + add, so a moved guarded file still
    shows up under its old name."""
    files = sorted({f.strip() for f in changed if f.strip()})
    if not files:
        return False, "no changed files to judge"
    guarded = [f for f in files if _matches(f, GUARDED)]
    if guarded:
        return False, "touches guarded files: " + ", ".join(guarded)
    other = [f for f in files if not _matches(f, CONTENT)]
    if other:
        return False, "not plain content: " + ", ".join(other)
    return True, "content only: " + ", ".join(files)


def main() -> int:
    """Changed paths on stdin, one per line. Exit 0 = may auto-merge,
    1 = needs the owner. The reason goes to stdout either way."""
    auto, reason = decide(sys.stdin.read().splitlines())
    print(reason)
    return 0 if auto else 1


if __name__ == "__main__":
    sys.exit(main())
