"""The systemd units `just install-services` installs are rendered for the checkout they run from."""

import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("render_units", ROOT / "scripts" / "render_units.py")
render_units = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(render_units)

SAMPLE = Path("/srv/build/checkouts/vinylgen-2")
SERVICE_TEMPLATE = (ROOT / "deploy" / "vinylgen-user.service").read_text()
TUNNEL_TEMPLATE = (ROOT / "deploy" / "vinylgen-tunnel-user.service").read_text()


def _values(unit: str, key: str) -> list[str]:
    return re.findall(rf"(?m)^{key}=(.*)$", unit)


class ServiceRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.unit = render_units.render_service(SERVICE_TEMPLATE, SAMPLE)

    def test_every_checkout_path_line_names_the_checkout(self) -> None:
        self.assertEqual(_values(self.unit, "WorkingDirectory"), [str(SAMPLE)])
        self.assertEqual(_values(self.unit, "EnvironmentFile"),
                         [f"{SAMPLE}/.vinylgen-data/vinylgen.env"])
        [exec_start] = _values(self.unit, "ExecStart")
        self.assertTrue(exec_start.startswith(f"{SAMPLE}/.venv/bin/uvicorn "), exec_start)
        self.assertEqual([value for value in _values(self.unit, "Environment")
                          if value.startswith("NUMBA_CACHE_DIR=")],
                         [f"NUMBA_CACHE_DIR={SAMPLE}/.tmp/numba-cache"])
        self.assertNotIn("%h/vinylgen", self.unit)

    def test_read_write_paths_are_the_checkout_state_and_the_t3api_cache(self) -> None:
        [line] = _values(self.unit, "ReadWritePaths")
        self.assertEqual(line.split(),
                         [f"{SAMPLE}/.vinylgen-data", f"{SAMPLE}/.tmp", "%h/.cache/t3api"])

    def test_only_the_checkout_paths_change(self) -> None:
        template_lines = SERVICE_TEMPLATE.splitlines()
        rendered_lines = self.unit.splitlines()
        self.assertEqual(len(rendered_lines), len(template_lines))
        for template, rendered in zip(template_lines, rendered_lines):
            self.assertEqual(rendered, template.replace("%h/vinylgen", str(SAMPLE)))

    def test_a_trailing_slash_on_the_checkout_is_dropped(self) -> None:
        self.assertEqual(render_units.render_service(SERVICE_TEMPLATE, Path(f"{SAMPLE}/")), self.unit)

    def test_a_path_a_unit_cannot_hold_is_refused(self) -> None:
        for bad in ("relative/vinylgen", "/home/a b/vinylgen", "/home/%u/vinylgen"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                render_units.render_service(SERVICE_TEMPLATE, Path(bad))

    def test_a_prefix_of_the_template_path_is_not_rewritten(self) -> None:
        unit = render_units.render_service(
            "WorkingDirectory=%h/vinylgen\nReadWritePaths=%h/vinylgen-other %h/vinylgen/.tmp\n", SAMPLE)
        self.assertEqual(unit, f"WorkingDirectory={SAMPLE}\n"
                               f"ReadWritePaths=%h/vinylgen-other {SAMPLE}/.tmp\n")


class TunnelRenderTests(unittest.TestCase):
    def test_only_the_ssh_destination_changes(self) -> None:
        unit = render_units.render_tunnel(TUNNEL_TEMPLATE, "samwise")
        template_lines = TUNNEL_TEMPLATE.splitlines()
        rendered_lines = unit.splitlines()
        self.assertEqual(len(rendered_lines), len(template_lines))
        changed = [(t, r) for t, r in zip(template_lines, rendered_lines) if t != r]
        self.assertEqual(len(changed), 1)
        template, rendered = changed[0]
        self.assertTrue(template.startswith("ExecStart=/usr/bin/ssh "))
        self.assertEqual(template.split()[:-1], rendered.split()[:-1])
        self.assertEqual((template.split()[-1], rendered.split()[-1]), ("edge-host", "samwise"))

    def test_a_destination_that_is_not_a_host_is_refused(self) -> None:
        for bad in ("", "-oProxyCommand=x", "two words", "host;rm"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                render_units.render_tunnel(TUNNEL_TEMPLATE, bad)


class InstallTests(unittest.TestCase):
    def test_both_units_are_written_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            unit_dir = Path(scratch) / "systemd" / "user"
            written = render_units.install(SAMPLE, "samwise", unit_dir)
            self.assertEqual(sorted(path.name for path in written.values()),
                             ["vinylgen-tunnel.service", "vinylgen.service"])
            self.assertEqual((unit_dir / "vinylgen.service").read_text(),
                             render_units.render_service(SERVICE_TEMPLATE, SAMPLE))
            self.assertEqual((unit_dir / "vinylgen-tunnel.service").stat().st_mode & 0o777, 0o644)
            self.assertEqual(sorted(p.name for p in unit_dir.iterdir()),
                             ["vinylgen-tunnel.service", "vinylgen.service"])

    def test_nothing_is_written_when_either_unit_fails_to_render(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            unit_dir = Path(scratch) / "units"
            with self.assertRaises(ValueError):
                render_units.install(SAMPLE, "-bad", unit_dir)
            self.assertFalse(unit_dir.exists())

    def test_the_tunnel_host_comes_from_the_env_file_and_must_be_set(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            env = Path(scratch) / "vinylgen.env"
            env.write_text("VINYLGEN_PUBLIC_URL=https://x.example\n")
            unit_dir = Path(scratch) / "units"
            argv = ["--checkout", str(SAMPLE), "--env-file", str(env), "--unit-dir", str(unit_dir)]
            self.assertEqual(render_units.main(argv), 1)
            self.assertFalse(unit_dir.exists())
            env.write_text(env.read_text() + "VINYLGEN_TUNNEL_HOST=samwise\n")
            self.assertEqual(render_units.main(argv), 0)
            self.assertIn(" samwise\n", (unit_dir / "vinylgen-tunnel.service").read_text())


class JustfileTests(unittest.TestCase):
    def test_install_services_renders_for_the_justfile_directory(self) -> None:
        justfile = (ROOT / "justfile").read_text()
        recipe = justfile.split("\ninstall-services:", 1)[1].split("\n\n", 1)[0]
        self.assertIn('scripts/render_units.py --checkout "{{justfile_directory()}}"', recipe)
        self.assertNotIn("install -Dm644 deploy/", recipe)
        self.assertIn("Preserving existing .vinylgen-data/vinylgen.env", recipe)


if __name__ == "__main__":
    unittest.main()
