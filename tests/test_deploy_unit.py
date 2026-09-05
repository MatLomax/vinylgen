import re
import unittest
from pathlib import Path


class ServiceUnitSandboxTests(unittest.TestCase):
    """The user service runs under ProtectSystem=strict, so every path a runtime
    dependency must write to has to sit inside ReadWritePaths."""

    def setUp(self) -> None:
        self.unit = (Path(__file__).resolve().parents[1]
                     / "deploy" / "vinylgen-user.service").read_text()

    def _service_value(self, key: str) -> str:
        match = re.search(rf"^{key}=(.*)$", self.unit, re.MULTILINE)
        self.assertIsNotNone(match, f"{key} missing from the service unit")
        return match.group(1).strip()

    def test_numba_cache_dir_is_writable_under_strict_protection(self) -> None:
        self.assertEqual(self._service_value("ProtectSystem"), "strict")
        writable = self._service_value("ReadWritePaths").split()
        environment = re.findall(r"^Environment=(.*)$", self.unit, re.MULTILINE)
        cache_dirs = [value.split("=", 1)[1] for value in environment
                      if value.startswith("NUMBA_CACHE_DIR=")]
        self.assertEqual(len(cache_dirs), 1, "exactly one NUMBA_CACHE_DIR expected")
        cache_dir = cache_dirs[0]
        self.assertTrue(
            any(cache_dir == root or cache_dir.startswith(root.rstrip("/") + "/")
                for root in writable),
            f"NUMBA_CACHE_DIR {cache_dir} is outside ReadWritePaths {writable}")


if __name__ == "__main__":
    unittest.main()
