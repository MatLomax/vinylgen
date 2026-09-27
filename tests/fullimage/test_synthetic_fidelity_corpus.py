"""The full synthetic fidelity corpus (``regression.all_runs``), opt-in: every
committed case in both tiers, with the checks that each case's bounds can fail.
The default run traces only the gate slice (``tests/test_synthetic_fidelity.py``).
The classes and what they assert are in ``tests/synthetic_cases.py``."""

from __future__ import annotations

import unittest

from synthetic_cases import generate
from vinylgen.fidelity.regression import all_runs

globals().update(generate(all_runs(), __name__, bound_checks=True))


if __name__ == "__main__":
    unittest.main()
