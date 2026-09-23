import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from vinylgen.cli_trace import parser, settings_from_args
from vinylgen.contracts import TraceSettings


class LegacyConfigTests(unittest.TestCase):
    def settings(self, config: dict, *flags: str) -> TraceSettings:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            path = Path(name) / "config.json"
            path.write_text(json.dumps(config))
            args = parser().parse_args(["outputs", "--config", str(path), *flags])
            settings, _ = settings_from_args(args)
        return settings

    def legacy_block(self, **changes) -> dict:
        # The settings block of an older trace.json: every field it recorded, with
        # the curve_optimization flag and without corner_straightness.
        block = asdict(TraceSettings())
        del block["corner_straightness"]
        block["curve_optimization"] = True
        block.update(changes)
        return block

    def test_false_flag_keeps_recorded_corners_and_zeroes_the_deviation(self):
        settings = self.settings(self.legacy_block(curve_optimization=False, straight_line_max_deviation=1.2))
        self.assertEqual(settings.straight_line_max_deviation, 0.0)
        self.assertEqual(settings.corner_straightness, 1.2)

    def test_false_flag_without_a_recorded_deviation_uses_the_default(self):
        settings = self.settings({"curve_optimization": False})
        self.assertEqual(settings.straight_line_max_deviation, 0.0)
        self.assertEqual(settings.corner_straightness, 1.6)

    def test_false_flag_keeps_an_explicit_corner_straightness(self):
        settings = self.settings({"curve_optimization": False, "straight_line_max_deviation": 1.2,
                                  "corner_straightness": .5})
        self.assertEqual((settings.straight_line_max_deviation, settings.corner_straightness), (0.0, .5))

    def test_true_flag_is_dropped_and_changes_nothing(self):
        settings = self.settings(self.legacy_block(straight_line_max_deviation=1.2))
        self.assertEqual((settings.straight_line_max_deviation, settings.corner_straightness), (1.2, 1.2))
        self.assertFalse(hasattr(settings, "curve_optimization"))

    def test_cli_flags_still_override_the_upgraded_config(self):
        settings = self.settings(self.legacy_block(curve_optimization=False),
                                 "--straight-line-max-deviation", ".3")
        self.assertEqual((settings.straight_line_max_deviation, settings.corner_straightness), (.3, 1.6))

    def test_other_unknown_keys_are_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown config setting"):
            self.settings({"not_a_setting": 1})


if __name__ == "__main__":
    unittest.main()
