"""(Re)write the committed synthetic fidelity cases.

Writes ``tests/synthetic/fixed/`` from ``families.fixed_set()`` and
``tests/synthetic/palette-stage/`` from ``families.palette_stage_set()``: a
``<case_id>.json`` (case id, ``min_feature_px``, tags and ``Scene.to_json``) and a
``<case_id>.svg`` (``Scene.to_svg``) per case. Case files no longer generated are
deleted. ``tests/test_fidelity_families.py`` checks that the committed files match
the generator byte for byte.

Usage: generate_synthetic_fixed.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vinylgen.fidelity.families import (FIXED_DIR, PALETTE_STAGE_DIR,  # noqa: E402
                                        fixed_set, palette_stage_set, write_cases)


def main() -> int:
    for directory, cases in ((FIXED_DIR, fixed_set()), (PALETTE_STAGE_DIR, palette_stage_set())):
        written = write_cases(cases, ROOT / directory)
        print(f"{directory}: {len(cases)} cases, {len(written)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
