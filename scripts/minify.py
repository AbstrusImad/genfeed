#!/usr/bin/env python3
"""Produce a docstring/comment-stripped deploy bundle for pubdata-limited networks.

GenLayer stores a contract's source as rollup pubdata, so very large contracts
can exceed a network's per-transaction pubdata budget. The full library bundle
(``dist/price_oracle.bundle.py``) is ~70 KB — but most of that is
documentation. This script parses the bundle, removes module/class/function
docstrings (and, via :func:`ast.unparse`, every comment), re-prepends the
pinned runner header, and writes ``dist/price_oracle.min.py`` — byte-for-byte
equivalent in behaviour, typically ~60% smaller.

The minified file is what you deploy when a network rejects the full bundle
with ``BlockPubdataLimitReached`` / ``intrinsic gas too low``; the readable
``price_oracle.bundle.py`` stays the canonical, reviewable artifact.

Usage:  python scripts/minify.py   (run scripts/bundle.py first)
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dist" / "price_oracle.bundle.py"
OUT = ROOT / "dist" / "price_oracle.min.py"

_DEF_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _strip_docstrings(tree: ast.AST) -> None:
    """Drop the leading string-literal statement of every def/class/module."""
    for node in ast.walk(tree):
        if not isinstance(node, _DEF_NODES):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = body[1:] if len(body) > 1 else [ast.Pass()]


def main() -> None:
    lines = SRC.read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].startswith("# {"):
        raise SystemExit("error: bundle must start with a pinned runner header (run scripts/bundle.py first)")
    header = lines[0]

    tree = ast.parse("\n".join(lines))
    _strip_docstrings(tree)
    minified = ast.unparse(tree)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(header + "\n" + minified + "\n", encoding="utf-8", newline="\n")
    print(f"minified -> {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes, from {SRC.stat().st_size})")


if __name__ == "__main__":
    main()
