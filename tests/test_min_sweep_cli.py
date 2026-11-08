"""The trace CLI's minimum sweeps: ``--min-sweep-arc DEG`` and ``--min-sweep-ellipse DEG``, and the same keys in a
``--config``, as the web panel's minimum-sweep fields (``tests/test_min_sweep_settings.py``).

Each is a number of degrees in [0, 360], 0 (no minimum) by default, passed to ``TraceSettings`` as a float; a flag
overrides the config; a minimum above 0 on a kind given as off (``--half-arc off`` or a config's ``null``) is
refused, as is one outside [0, 360] or not a number, with exit 2 before anything is traced. A CLI trace at 270 / 270
records the minima in ``trace.json`` and the map header (t3api 0.29.0's canonical ``min_sweep``).
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_kind_tolerance_settings import SETTING_DEFAULT, header_setting, record
from test_t3_trace import SCRATCH, build_subject
from vinylgen.contracts import TraceSettings


class CliMinSweepFlagsTest(unittest.TestCase):
    def setUp(self) -> None:
        SCRATCH.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def parsed(self, *flags: str) -> TraceSettings:
        from vinylgen.cli_trace import parser, settings_from_args
        settings, _, _ = settings_from_args(parser().parse_args(["outputs", *flags]))
        return settings

    def with_config(self, config: dict, *flags: str) -> TraceSettings:
        path = self.root / "config.json"
        path.write_text(json.dumps(config))
        return self.parsed("--config", str(path), *flags)

    def refused(self, *flags: str) -> None:
        """``main`` at ``flags`` exits 2 and traces nothing."""
        from vinylgen.cli_trace import main
        subject = build_subject(Path(tempfile.mkdtemp(dir=self.root)) / "subject")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch("sys.argv", ["svg_trace.py", str(subject), *flags]), \
                self.assertRaises(SystemExit) as exit:
            main()
        self.assertEqual(exit.exception.code, 2)
        self.assertFalse((subject / "map.json").exists(), "nothing is traced")

    def test_no_flag_leaves_both_minima_at_0(self):
        settings = self.parsed()
        self.assertEqual((settings.min_sweep_arc, settings.min_sweep_ellipse), (0.0, 0.0))
        self.assertEqual(settings, TraceSettings())

    def test_the_two_flags_set_each_kinds_minimum_as_a_float(self):
        settings = self.parsed("--min-sweep-arc", "270", "--min-sweep-ellipse", "90.5")
        self.assertEqual(settings, TraceSettings(min_sweep_arc=270.0, min_sweep_ellipse=90.5))
        self.assertIsInstance(settings.min_sweep_arc, float)

    def test_the_bounds_0_and_360_are_accepted(self):
        for value in ("0", "360"):
            with self.subTest(value=value):
                self.assertEqual(self.parsed("--min-sweep-arc", value).min_sweep_arc, float(value))
                self.assertEqual(self.parsed("--min-sweep-ellipse", value).min_sweep_ellipse, float(value))

    def test_an_off_kind_takes_0_and_its_other_kind_keeps_its_minimum(self):
        self.assertEqual(self.parsed("--half-arc", "off", "--min-sweep-arc", "0", "--min-sweep-ellipse", "270"),
                         TraceSettings(half_arc=None, min_sweep_ellipse=270.0))

    def test_out_of_range_values_and_a_minimum_on_an_off_kind_are_refused(self):
        refused = [["--min-sweep-arc", "-1"], ["--min-sweep-arc", "360.5"], ["--min-sweep-ellipse", "-0.5"],
                   ["--min-sweep-ellipse", "400"], ["--min-sweep-arc", "nan"], ["--min-sweep-arc", "inf"],
                   ["--min-sweep-arc", "270deg"],
                   ["--half-arc", "off", "--min-sweep-arc", "270"],
                   ["--half-ellipse", "off", "--min-sweep-ellipse", "1"]]
        for flags in refused:
            with self.subTest(flags=flags):
                self.refused(*flags)

    def test_a_config_round_trips_the_minima_and_flags_override_it(self):
        config = {"settings": record(min_sweep_arc=270, min_sweep_ellipse=300.5)}
        settings = self.with_config(config)
        self.assertEqual(settings, TraceSettings(min_sweep_arc=270.0, min_sweep_ellipse=300.5))
        self.assertIsInstance(settings.min_sweep_arc, float)
        self.assertEqual(self.with_config(config, "--min-sweep-arc", "0"),
                         TraceSettings(min_sweep_arc=0.0, min_sweep_ellipse=300.5))

    def test_a_config_without_the_minima_reads_them_as_0(self):
        config = {"settings": {key: value for key, value in record().items() if not key.startswith("min_sweep")}}
        self.assertEqual(self.with_config(config), TraceSettings())

    def test_a_config_with_a_bad_minimum_or_one_on_an_off_kind_is_refused(self):
        bad = [record(min_sweep_arc=-1), record(min_sweep_ellipse=361), record(min_sweep_arc=True),
               record(min_sweep_arc="270"), record(min_sweep_arc=None),
               record(half_arc=None, min_sweep_arc=270), record(half_ellipse=None, min_sweep_ellipse=10)]
        for settings in bad:
            with self.subTest(settings=settings):
                path = self.root / "config.json"
                path.write_text(json.dumps({"settings": settings}))
                self.refused("--config", str(path))

    def test_a_flag_minimum_on_a_kind_the_config_turns_off_is_refused(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"settings": record(half_ellipse=None)}))
        self.refused("--config", str(path), "--min-sweep-ellipse", "90")

    def test_a_cli_trace_at_270_records_the_minima_and_reaches_t3(self):
        from vinylgen.cli_trace import main
        subject = build_subject(self.root / "subject")
        argv = ["svg_trace.py", str(subject), "--min-sweep-arc", "270", "--min-sweep-ellipse", "270"]
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
            main()
        setting = {**SETTING_DEFAULT, "min_sweep": {"arc": 270.0, "ellipse": 270.0}}
        map_bytes = (subject / "map.json").read_bytes()
        audit = json.loads((subject / "trace.json").read_text())
        self.assertEqual(header_setting(map_bytes), setting)
        self.assertEqual(audit["backend"]["setting"], setting)
        self.assertEqual(audit["settings"], record(min_sweep_arc=270.0, min_sweep_ellipse=270.0))
        self.assertTrue(audit["validation"]["edge"]["passed"])


if __name__ == "__main__":
    unittest.main()
