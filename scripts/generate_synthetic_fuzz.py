"""(Re)write the committed pinned fuzz cases.

Reads ``tests/synthetic/fuzz-seeds.json`` and writes, for every pinned row, the
case ``fuzz.fuzz_case(seed, size, kind)`` to ``tests/synthetic/fuzz/`` as
``<case_id>.json`` (``fuzz.case_json``) and ``<case_id>.svg`` (``Scene.to_svg``).
Case files no longer pinned are deleted. ``tests/test_fidelity_fuzz.py`` checks
that the committed files match the generator byte for byte, so run this only
when a generator change is meant to change the pinned ground truth.

Usage: generate_synthetic_fuzz.py [--check]

``--check`` writes nothing and exits 1 when any committed file differs.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vinylgen.fidelity.fuzz import (PINNED_DIR, SEEDS_PATH, case_json,  # noqa: E402
                                    fuzz_case, pinned_rows)


def _files(row: dict) -> dict[str, str]:
    case = fuzz_case(row["seed"], row["size"], row["kind"])
    return {f"{case.case_id}.json": case_json(case), f"{case.case_id}.svg": case.scene.to_svg()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="compare with the committed files instead of writing them")
    args = parser.parse_args()
    rows = pinned_rows(SEEDS_PATH)
    files: dict[str, str] = {}
    with ProcessPoolExecutor() as pool:
        for generated in pool.map(_files, rows):
            files.update(generated)
    directory = Path(PINNED_DIR)
    existing = {path.name for pattern in ("*.json", "*.svg") for path in directory.glob(pattern)} \
        if directory.exists() else set()
    if args.check:
        stale = sorted(existing - set(files))
        changed = sorted(name for name, text in files.items()
                         if not (directory / name).exists()
                         or (directory / name).read_text(encoding="utf-8") != text)
        for name in changed:
            print(f"differs: {name}")
        for name in stale:
            print(f"not pinned: {name}")
        return 1 if changed or stale else 0
    directory.mkdir(parents=True, exist_ok=True)
    for name in sorted(existing - set(files)):
        (directory / name).unlink()
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")
    print(f"{directory.relative_to(ROOT)}: {len(rows)} cases, {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
