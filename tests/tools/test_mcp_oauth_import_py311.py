"""``tools.mcp_oauth`` must import on every supported interpreter (``requires-python >= 3.11``).

``threading.Lock`` (and ``RLock``) is a factory function, not a class, before Python 3.13. An
eagerly evaluated annotation ``threading.Lock | None`` therefore raises
``TypeError: unsupported operand type(s) for |: 'builtin_function_or_method' and 'NoneType'``
while the module is being imported on 3.11 / 3.12. fbbab5fb37 added one to
``_paste_callback_reader``; every script that imports ``tools.mcp_oauth`` from a 3.11 venv died
with it (an hourly meeting sync ran 8 times with exit 1 before anyone noticed). The suite runs
on a newer interpreter, where the expression is legal, so the check is on the syntax tree.
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_FACTORIES = {"Lock", "RLock"}


def _eager_lock_unions(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    postponed = any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body)
    if postponed:
        return []
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            annotations = [a.annotation for a in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg)
                           if a is not None and a.annotation is not None]
            if node.returns is not None:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations = [node.annotation]
        else:
            continue
        for annotation in annotations:
            for sub in ast.walk(annotation):
                if not (isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr)):
                    continue
                for side in (sub.left, sub.right):
                    if (isinstance(side, ast.Attribute) and side.attr in _FACTORIES
                            and isinstance(side.value, ast.Name) and side.value.id == "threading"):
                        hits.append(f"{path.relative_to(REPO)}:{sub.lineno}")
    return hits


def test_the_detector_sees_the_regression(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("import threading\n\ndef f(lock: threading.Lock | None = None) -> None: ...\n")
    quoted = tmp_path / "quoted.py"
    quoted.write_text('import threading\n\ndef f(lock: "threading.Lock | None" = None) -> None: ...\n')
    postponed = tmp_path / "postponed.py"
    postponed.write_text("from __future__ import annotations\nimport threading\n\ndef f(lock: threading.Lock | None = None) -> None: ...\n")
    global REPO
    real, REPO = REPO, tmp_path
    try:
        assert _eager_lock_unions(bad) == ["bad.py:3"]
        assert _eager_lock_unions(quoted) == []
        assert _eager_lock_unions(postponed) == []
    finally:
        REPO = real


@pytest.mark.parametrize("module", ["tools/mcp_oauth.py", "tools/mcp_oauth_manager.py", "tools/mcp_tool.py"])
def test_mcp_oauth_modules_evaluate_no_lock_union_at_import(module):
    assert _eager_lock_unions(REPO / module) == []
