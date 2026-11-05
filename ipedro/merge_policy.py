"""Which /evolve pull requests may merge without the owner looking.

The rule the owner approved: a PR auto-merges only when the workflow's own
test run passes AND every file it changes is plain content — prompts,
persona text, canned lines, the GIF table, the tests that pin them. Anything
else waits for the owner. It's an allowlist, so it fails closed: a file this
list has never heard of (a new module, a moved one) waits too.

What makes this a guardrail rather than a suggestion:

* The workflow runs MAIN's copy of this file, never the PR branch's — a PR
  that loosened these lists would otherwise get to approve itself. That's
  also why it imports nothing outside the standard library: it runs as a
  lone file pulled out of main, detached from the package.
* GUARDED is checked first and wins over CONTENT, and a test pins that no
  CONTENT path is ever a GUARDED one.
* "Content" is a claim about what a file CONTAINS, so it is checked, not
  assumed. A content module is imported by the bot at startup; if a change
  could put a function or an import in one, "content only" would mean
  "arbitrary code, unreviewed". So each data module is parsed and must hold
  nothing but constants (see ``data_violation``). Logic that decides
  anything lives in a guarded file; the data it reads lives in a checked one.
* Tests are code that runs in CI, so they get less trust than data: a PR may
  ADD a test file, and may edit the few tests that pin nothing but content,
  but editing any other existing test (where a pin could be weakened) or
  deleting anything waits for the owner. Paths are matched exactly:
  fnmatch's ``*`` crosses ``/``, which once let ``tests/test_x/conftest.py``
  through as a "test".
"""

from __future__ import annotations

import ast
import re
import sys
from fnmatch import fnmatchcase
from pathlib import Path

# Modules that hold DATA only and so may change without review — as long as
# they stay data, which decide() verifies by parsing the PR's copy.
DATA_MODULES: tuple[str, ...] = (
    "ipedro/prompts.py",
    "ipedro/dale_gif_seeds.py",
    "ipedro/handlers/automod_bits.py",
)

# Existing tests a content change may have to touch: they pin what the
# content says (the canned bits, the prompts' placeholders) and nothing
# about who may do what.
CONTENT_TESTS: tuple[str, ...] = (
    "tests/test_automod.py",
    "tests/test_prompts_and_challenge_pool.py",
)

# Plain content: changing these changes what the bot SAYS, not who it
# answers, what it remembers, what it can reach, or who may do what. A NEW
# test file also counts (see NEW_TEST).
CONTENT: tuple[str, ...] = DATA_MODULES + CONTENT_TESTS

# A new test file directly under tests/ — never a subdirectory, which is how
# a conftest.py or a helper module would slip in.
NEW_TEST = re.compile(r"tests/test_[A-Za-z0-9_]+\.py")

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
    "ipedro/hub.py",
    "ipedro/persona_gen.py",
    "ipedro/plain_flavor.py",
    # who may do what, and what the bot can see about itself
    "ipedro/auth.py",
    "ipedro/config.py",
    "ipedro/handlers/common.py",
    "ipedro/introspection.py",
    "ipedro/capabilities.py",
    "ipedro/ratelimit.py",
    "ipedro/net_safety.py",
    # the core: who it answers, what it remembers, how it talks to the model
    "ipedro/addressed.py",
    "ipedro/chat_policy.py",
    "ipedro/handlers/chat.py",
    "ipedro/memory/*",
    "ipedro/openai_client.py",
    "ipedro/db/*",
    "ipedro/personas.py",
    # what content files may name or reach
    "ipedro/handlers/automod.py",
    "ipedro/automod_types.py",
    "ipedro/automod_media.py",
    "ipedro/dale_gifs.py",
    # dependencies and deployment: a supply-chain change is never "content"
    "requirements*.txt",
    "docker/*",
    ".env.example",
    # the harness every test runs in — at any depth — and the tests that
    # guard all of the above
    "pytest.ini",
    "*conftest.py",
    "tests/test_merge_policy.py",
    "tests/test_evolve.py",
    "tests/test_auth.py",
    "tests/test_common.py",
    "tests/test_introspection.py",
    "tests/test_automod_rules.py",
    "tests/test_bots.py",
    "tests/test_hub.py",
    "tests/test_identity.py",
    "tests/test_config_auth.py",
    "tests/test_capabilities.py",
    "tests/test_net_safety.py",
    "tests/test_ratelimit.py",
    "tests/test_fixname_auth.py",
    "tests/test_deploy_files.py",
)


# ── is this file still just data? ────────────────────────────────────────────

# The only imports a data module may have: the future import, `re` for
# re.compile, and the two record types the automod table builds.
_DATA_IMPORTS: dict[str, frozenset[str] | None] = {
    "__future__": None,
    "ipedro.automod_types": frozenset({"DaleGif", "MediaResponse"}),
}
_DATA_PLAIN_IMPORTS = frozenset({"re"})
# The only calls: compiling a literal pattern, and building those records.
_DATA_CONSTRUCTORS = frozenset({"DaleGif", "MediaResponse"})
_RE_FLAG = re.compile(r"[A-Z]+")


def _expr_violation(node: ast.AST) -> str | None:
    """Why ``node`` isn't a plain data expression, or None if it is."""
    if isinstance(node, ast.Constant):
        return None
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return next(filter(None, map(_expr_violation, node.elts)), None)
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if key is None:
                return "a `**` dict unpacking"
            bad = _expr_violation(key) or _expr_violation(value)
            if bad:
                return bad
        return None
    if isinstance(node, ast.Name):
        return None if isinstance(node.ctx, ast.Load) else "a name being rebound"
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.BitOr)):
        return _expr_violation(node.left) or _expr_violation(node.right)
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "re" and _RE_FLAG.fullmatch(node.attr)):
        return None                                    # re.IGNORECASE and friends
    if isinstance(node, ast.Call):
        func = node.func
        is_compile = (isinstance(func, ast.Attribute) and func.attr == "compile"
                      and isinstance(func.value, ast.Name) and func.value.id == "re")
        is_record = isinstance(func, ast.Name) and func.id in _DATA_CONSTRUCTORS
        if not (is_compile or is_record):
            return f"a call to `{ast.unparse(func)}`"
        if any(isinstance(a, ast.Starred) for a in node.args):
            return "a `*` argument"
        if any(k.arg is None for k in node.keywords):
            return "a `**` argument"
        parts = [*node.args, *(k.value for k in node.keywords)]
        return next(filter(None, map(_expr_violation, parts)), None)
    return f"{type(node).__name__} (not a constant)"


def data_violation(source: str) -> str | None:
    """Why ``source`` isn't a data-only module, or None if it is.

    Data-only means: a docstring, `from __future__`, `import re`, the record
    types, and assignments whose values are constants — strings, numbers,
    tuples, lists, dicts, `re.compile("literal", re.FLAG)` and the two record
    constructors. No function, class, loop, condition, comprehension, f-string,
    or any other call or import, so importing the module can only build values.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"doesn't parse (line {exc.lineno})"
    for stmt in tree.body:
        where = f"line {stmt.lineno}"
        if isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue                                # a docstring
            return f"{where}: an expression statement"
        if isinstance(stmt, ast.ImportFrom):
            allowed = _DATA_IMPORTS.get(stmt.module or "", False)
            if stmt.level or allowed is False:
                return f"{where}: imports from {stmt.module!r}"
            names = {a.name for a in stmt.names}
            if allowed is not None and not names <= allowed:
                return f"{where}: imports {sorted(names - allowed)}"
            continue
        if isinstance(stmt, ast.Import):
            if all(a.name in _DATA_PLAIN_IMPORTS and a.asname is None for a in stmt.names):
                continue
            return f"{where}: imports {[a.name for a in stmt.names]}"
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets, value = [stmt.target], stmt.value
        else:
            return f"{where}: a {type(stmt).__name__} (not data)"
        if not all(isinstance(t, ast.Name) for t in targets):
            return f"{where}: assigns to something other than a plain name"
        if value is not None:
            bad = _expr_violation(value)
            if bad:
                return f"{where}: {bad}"
    return None


# ── the verdict ──────────────────────────────────────────────────────────────

# `git diff --raw --no-renames`: ":<old mode> <new mode> <old sha> <new sha> <status>\t<path>"
_RAW = re.compile(r"^:(\d{6}) (\d{6}) \w+ \w+ ([A-Z])\d*\t(.+)$")
_PLAIN_FILE_MODE = "100644"
_NO_FILE = "000000"


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(path, p) for p in patterns)


def _parse(changed: list[str]) -> dict[str, tuple[str, str, str]]:
    """path -> (status, old mode, new mode). A bare path (no `git diff --raw`
    prefix) is read as an ordinary modification of an ordinary file."""
    out: dict[str, tuple[str, str, str]] = {}
    for line in changed:
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        m = _RAW.match(line)
        if m:
            old, new, status, path = m.groups()
            out[path] = (status, old, new)
        else:
            out[line.strip()] = ("M", _PLAIN_FILE_MODE, _PLAIN_FILE_MODE)
    return out


def decide(changed: list[str], root: Path | None = None) -> tuple[bool, str]:
    """(may_auto_merge, reason). ``changed`` is every path the PR touches, as
    ``git diff --raw --no-renames`` prints it (renames split into delete +
    add, so a moved guarded file still shows up under its old name) or plain
    paths. ``root`` is the PR's checkout, where the data modules are read
    to verify they are still data; None judges by path and mode alone."""
    changes = _parse(changed)
    if not changes:
        return False, "no changed files to judge"
    files = sorted(changes)

    guarded = [f for f in files if _matches(f, GUARDED)]
    if guarded:
        return False, "touches guarded files: " + ", ".join(guarded)

    deleted = [f for f in files if changes[f][0] == "D"]
    if deleted:
        return False, "deletes files: " + ", ".join(deleted)

    odd = [f for f in files if changes[f][2] != _PLAIN_FILE_MODE
           or changes[f][1] not in (_NO_FILE, _PLAIN_FILE_MODE)]
    if odd:
        return False, "not ordinary files (symlink, submodule or mode change): " + ", ".join(odd)

    edited_tests, other = [], []
    for f in files:
        if f in CONTENT:
            continue
        if NEW_TEST.fullmatch(f):
            if changes[f][0] != "A":
                edited_tests.append(f)      # an existing test that pins more than content
            continue                        # (a brand-new test file is fine)
        other.append(f)
    if other:
        return False, "not plain content: " + ", ".join(other)
    if edited_tests:
        return False, "edits existing tests, which only the owner may: " + ", ".join(edited_tests)

    if root is not None:
        for f in files:
            if f not in DATA_MODULES:
                continue
            try:
                source = (root / f).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return False, f"can't read {f} to check it is still data ({type(exc).__name__})"
            bad = data_violation(source)
            if bad:
                return False, f"{f} is no longer data only: {bad}"
    return True, "content only: " + ", ".join(files)


def main(argv: list[str] | None = None) -> int:
    """Changed paths on stdin (``git diff --raw --no-renames`` lines or bare
    paths); ``--root DIR`` is the PR checkout (default: the current
    directory). Exit 0 = may auto-merge, 1 = needs the owner. The reason goes
    to stdout either way."""
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path.cwd()
    if args[:1] == ["--root"] and len(args) == 2:
        root = Path(args[1])
    elif args:
        print("usage: merge_policy.py [--root DIR] < changed-paths")
        return 2
    auto, reason = decide(sys.stdin.read().splitlines(), root=root)
    print(reason)
    return 0 if auto else 1


if __name__ == "__main__":
    sys.exit(main())
