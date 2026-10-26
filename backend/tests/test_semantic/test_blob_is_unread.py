# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Nothing in app/ reads Connection.semantic_model any more.

The column stays in place for one release as an unread fallback and is dropped
by a later migration. Until then, this test is what "unread" means.

Parses each file's AST rather than grepping: only ``ast.Attribute`` nodes
named ``semantic_model`` (and ``getattr(x, "semantic_model")`` calls) count,
so docstrings and comments that mention the column never trip it, and the
column's own declaration in ``models/connection.py`` (an ``AnnAssign``) does
not either. Every ``.semantic_model`` attribute access is flagged unless its
base name is in ``_NON_CONNECTION_BASES`` — the inverted direction, so a read
through a novel variable name is caught rather than silently permitted.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"

# Bases known to be something other than a Connection ORM row. Empty today: the
# CE has no dataclass carrying a same-named field. Add a name here, with a
# comment saying what it is, when one appears.
_NON_CONNECTION_BASES: frozenset[str] = frozenset()


def _offending_lines(path: Path, source: str) -> list[int]:
    tree = ast.parse(source, filename=str(path))
    linenos: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "semantic_model":
            base = node.value
            if isinstance(base, ast.Name) and base.id in _NON_CONNECTION_BASES:
                continue
            linenos.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "semantic_model"
        ):
            linenos.append(node.lineno)
    return linenos


def test_no_module_reads_the_semantic_blob() -> None:
    offenders: list[str] = []
    for path in sorted(_APP.rglob("*.py")):
        source = path.read_text()
        linenos = _offending_lines(path, source)
        if not linenos:
            continue
        lines = source.splitlines()
        rel = path.relative_to(_APP).as_posix()
        for lineno in linenos:
            offenders.append(f"{rel}:{lineno}: {lines[lineno - 1].strip()}")
    assert not offenders, "Connection.semantic_model is read here:\n" + "\n".join(offenders)
