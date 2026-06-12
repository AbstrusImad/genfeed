#!/usr/bin/env python3
"""Bundle the connectors library and the example contract into one file.

GenLayer deployment tools commonly take a single contract file. This script
inlines ``genlayer_free_connectors.py`` into ``price_oracle.py`` and writes a
self-contained, deploy-ready artifact to ``dist/price_oracle.bundle.py``:

    line 1   pinned runner header (taken from the contract)
    then     the full library source
    then     the contract source (header and library import removed)

Usage:
    python scripts/bundle.py
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "genlayer_free_connectors.py"
CONTRACT = ROOT / "price_oracle.py"
OUTPUT = ROOT / "dist" / "price_oracle.bundle.py"

LIB_IMPORT_PREFIX = "from genlayer_free_connectors import"


def main() -> None:
    contract_lines = CONTRACT.read_text(encoding="utf-8").splitlines()
    if not contract_lines or not contract_lines[0].startswith("# {"):
        raise SystemExit("error: contract must start with a pinned runner header")

    header = contract_lines[0]

    body: list[str] = []
    for line in contract_lines[1:]:
        if line.startswith(LIB_IMPORT_PREFIX):
            body.append("# (genlayer_free_connectors inlined above by scripts/bundle.py)")
        else:
            body.append(line)

    library_source = LIBRARY.read_text(encoding="utf-8")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        header + "\n" + library_source + "\n\n" + "\n".join(body) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"bundled -> {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
