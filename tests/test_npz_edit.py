from __future__ import annotations

import io
import unittest

import numpy as np

from vinylgen.web.npz_edit import (
    reuse_member_supported,
    savez_bytes,
    write_npz_reusing_member,
)


class NpzMemberReuseTest(unittest.TestCase):
    def _source(self) -> tuple[bytes, dict[str, np.ndarray], np.ndarray]:
        rng = np.random.default_rng(7)
        costs = (rng.random((40, 50, 4), dtype=np.float32) * 40).astype(np.float16)
        arrays = {
            "labels": rng.integers(0, 4, size=(40, 50), dtype=np.int16),
            "palette_rgb": rng.integers(0, 255, size=(4, 3), dtype=np.uint8),
            "contract_version": np.array(3, dtype=np.int16),
        }
        return savez_bytes(palette_costs=costs, **arrays), arrays, costs

    def test_reuse_is_supported_on_this_interpreter(self) -> None:
        # The whole point of the fast path is that it is available here; if a future
        # interpreter drops the internals, this fails loudly rather than silently
        # degrading every edit to the slow path.
        self.assertTrue(reuse_member_supported())

    def test_reused_member_matches_a_full_reencode(self) -> None:
        source, _, costs = self._source()
        # A label edit rewrites the small members and carries the cost volume over.
        fresh = {
            "labels": np.zeros((40, 50), dtype=np.int16),
            "palette_rgb": np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]], dtype=np.uint8),
            "contract_version": np.array(3, dtype=np.int16),
        }
        fast = io.BytesIO()
        write_npz_reusing_member(fast, source=source, reuse="palette_costs", fresh=fresh)
        slow = savez_bytes(palette_costs=costs, **fresh)

        with np.load(io.BytesIO(fast.getvalue())) as reused, np.load(io.BytesIO(slow)) as plain:
            self.assertEqual(set(reused.files), set(plain.files))
            for name in plain.files:
                np.testing.assert_array_equal(reused[name], plain[name], name)
            # The carried cost member is byte-identical to the parent's, dtype included.
            np.testing.assert_array_equal(reused["palette_costs"], costs)
            self.assertEqual(reused["palette_costs"].dtype, np.float16)

    def test_missing_member_raises_for_the_caller_to_fall_back(self) -> None:
        source, _, _ = self._source()
        with self.assertRaises(Exception):
            write_npz_reusing_member(
                io.BytesIO(), source=source, reuse="not_a_member",
                fresh={"labels": np.zeros((2, 2), dtype=np.int16)})


if __name__ == "__main__":
    unittest.main()
