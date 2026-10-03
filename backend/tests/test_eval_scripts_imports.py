"""The offline eval scripts must keep running standalone.

Each script in `scripts/embedding_eval/` runs with `uv run --script`, so it gets
only the dependencies in its own PEP 723 header, yet it imports app modules
(`ai_eval_service`, `relevance_service`, ...). When one of those modules starts
importing something the header does not list, the script breaks without any
test noticing: a `story_service` import in `ai_eval_service` once pulled in
pydantic and broke every script for a week.

This replays each script's `app.*` imports, including those of the sibling
scripts it imports, in a subprocess where every installed distribution outside
the header's dependencies (and theirs) is blocked.
"""
import ast
import json
import re
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "embedding_eval"
BACKEND_DIR = Path(__file__).resolve().parents[1]

_HEADER = re.compile(r"^# /// script\s*$(.*?)^# ///\s*$", re.M | re.S)


def _declared_deps(path: Path) -> list[str] | None:
    """The `dependencies` of a PEP 723 header, or None for a script without one."""
    m = _HEADER.search(path.read_text(encoding="utf-8"))
    if not m:
        return None
    body = "\n".join(line[2:] if line.startswith("# ") else line[1:]
                     for line in m.group(1).splitlines())
    return tomllib.loads(body).get("dependencies", [])


def _allowed_dists(deps: list[str]) -> set[str]:
    """The declared distributions plus everything they pull in, extras included."""
    allowed: set[str] = set()
    visited: set[tuple[str, frozenset[str]]] = set()
    queue = [Requirement(d) for d in deps]
    while queue:
        req = queue.pop()
        name = canonicalize_name(req.name)
        key = (name, frozenset(req.extras))
        if key in visited:
            continue
        visited.add(key)
        allowed.add(name)
        try:
            requires = metadata.requires(req.name) or []
        except metadata.PackageNotFoundError:
            continue  # declared but not installed here, so nothing to follow
        for spec in requires:
            sub = Requirement(spec)
            # "" stands for no extra: a plain platform or version marker.
            if sub.marker is None or any(
                    sub.marker.evaluate({"extra": e}) for e in {"", *req.extras}):
                queue.append(sub)
    return allowed


def _blocked_modules(allowed: set[str]) -> list[str]:
    blocked = []
    for module, dists in metadata.packages_distributions().items():
        if module == "app" or module in sys.stdlib_module_names:
            continue
        if not any(canonicalize_name(d) in allowed for d in dists):
            blocked.append(module)
    return sorted(blocked)


def _app_imports(path: Path, seen: set[Path] | None = None) -> list[str]:
    """`app.*` import statements of a script and of the sibling scripts it imports."""
    seen = seen if seen is not None else set()
    if path in seen:
        return []
    seen.add(path)
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        else:
            continue
        for name in names:
            if name == "app" or name.startswith("app."):
                out.append(ast.unparse(node))
            elif (SCRIPTS_DIR / f"{name}.py").exists():
                out.extend(_app_imports(SCRIPTS_DIR / f"{name}.py", seen))
    return list(dict.fromkeys(out))


_RUNNER = """
import importlib.abc, json, sys
blocked, statements, backend = json.loads(sys.argv[1])
blocked = set(blocked)

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.partition(".")[0] in blocked:
            raise ModuleNotFoundError(
                f"{name!r} is not in the script's dependencies", name=name)
        return None

sys.meta_path.insert(0, Block())
sys.path.insert(0, backend)
for statement in statements:
    exec(statement, {})
"""

SCRIPTS = sorted(p for p in SCRIPTS_DIR.glob("*.py") if _declared_deps(p) is not None)


def test_scripts_found():
    assert len(SCRIPTS) >= 5, f"no eval scripts with a PEP 723 header in {SCRIPTS_DIR}"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_app_imports_need_only_declared_deps(script: Path):
    statements = _app_imports(script)
    if not statements:
        pytest.skip("imports nothing from the app")
    blocked = _blocked_modules(_allowed_dists(_declared_deps(script)))
    result = subprocess.run(
        [sys.executable, "-c", _RUNNER, json.dumps([blocked, statements, str(BACKEND_DIR)])],
        capture_output=True, text=True, cwd=BACKEND_DIR, timeout=60,
    )
    assert result.returncode == 0, (
        f"{script.name} would fail under `uv run --script`: an app module it imports "
        f"needs a package its header does not list.\n{result.stderr[-2000:]}")
