#!/usr/bin/env python3
"""Render vinylgen's systemd user units from the templates in ``deploy/`` and install them.

The templates name the checkout ``%h/vinylgen`` and the tunnel's SSH destination ``edge-host``;
neither is this deployment's value. ``just install-services`` renders each template for the
checkout the justfile sits in and for the tunnel host set as ``VINYLGEN_TUNNEL_HOST`` in
``.vinylgen-data/vinylgen.env``, then installs the rendered files. The templates are not changed.

Every ``%h/vinylgen`` path (``WorkingDirectory``, ``EnvironmentFile``, ``ExecStart``,
``ReadWritePaths``, ``NUMBA_CACHE_DIR``) becomes the checkout's absolute path; paths outside the
checkout, such as the t3api cache ``%h/.cache/t3api``, are kept. The tunnel unit differs from its
template only in the SSH destination. Both units are rendered and checked before either is written.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_CHECKOUT = "%h/vinylgen"
TEMPLATE_TUNNEL_HOST = "edge-host"
SERVICE_TEMPLATE = ROOT / "deploy" / "vinylgen-user.service"
TUNNEL_TEMPLATE = ROOT / "deploy" / "vinylgen-tunnel-user.service"
SERVICE_UNIT = "vinylgen.service"
TUNNEL_UNIT = "vinylgen-tunnel.service"

# The template path as a whole path component: followed by "/", whitespace or the line's end.
_CHECKOUT_TOKEN = re.compile(re.escape(TEMPLATE_CHECKOUT) + r"(?=/|\s|$)", re.MULTILINE)
# A systemd unit value cannot hold an unquoted space, and "%" starts a specifier.
_UNSAFE_PATH = re.compile(r"[\s%\"'\\]")
_HOST = re.compile(r"[A-Za-z0-9_.@:\[\]-]+")


def render_service(template: str, checkout: Path) -> str:
    """The service unit with every ``%h/vinylgen`` path replaced by ``checkout``."""
    checkout_text = str(checkout)
    if not checkout.is_absolute():
        raise ValueError(f"the checkout path must be absolute: {checkout_text!r}")
    if _UNSAFE_PATH.search(checkout_text):
        raise ValueError(f"the checkout path cannot be written into a unit: {checkout_text!r}")
    checkout_text = checkout_text.rstrip("/") or "/"
    if not _CHECKOUT_TOKEN.search(template):
        raise ValueError(f"the service template names no {TEMPLATE_CHECKOUT} path")
    rendered = _CHECKOUT_TOKEN.sub(lambda _match: checkout_text, template)
    if _CHECKOUT_TOKEN.search(rendered):
        raise ValueError(f"a {TEMPLATE_CHECKOUT} path is left in the rendered service unit")
    return rendered


def render_tunnel(template: str, host: str) -> str:
    """The tunnel unit with its SSH destination ``edge-host`` replaced by ``host``."""
    if not _HOST.fullmatch(host) or host.startswith("-"):
        raise ValueError(f"not an SSH destination: {host!r}")
    lines = template.splitlines(keepends=True)
    hits = [index for index, line in enumerate(lines)
            if line.startswith("ExecStart=") and line.rstrip("\n").split()[-1] == TEMPLATE_TUNNEL_HOST]
    if len(hits) != 1:
        raise ValueError(f"the tunnel template has {len(hits)} ExecStart lines ending in "
                         f"{TEMPLATE_TUNNEL_HOST}; expected 1")
    line = lines[hits[0]]
    ending = line[len(line.rstrip("\n")):]
    lines[hits[0]] = line.rstrip("\n")[: -len(TEMPLATE_TUNNEL_HOST)] + host + ending
    return "".join(lines)


def env_value(env_file: Path, key: str) -> str:
    """The last ``key=value`` assignment of ``key`` in ``env_file``, or ``""``."""
    value = ""
    for line in env_file.read_text().splitlines():
        if line.startswith(key + "="):
            value = line.split("=", 1)[1].strip()
    return value


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def install(checkout: Path, host: str, unit_dir: Path) -> dict[str, Path]:
    """Render both units, then write them into ``unit_dir``; nothing is written if either fails."""
    rendered = {
        SERVICE_UNIT: render_service(SERVICE_TEMPLATE.read_text(), checkout),
        TUNNEL_UNIT: render_tunnel(TUNNEL_TEMPLATE.read_text(), host),
    }
    written = {}
    for name, text in rendered.items():
        _write(unit_dir / name, text)
        written[name] = unit_dir / name
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--checkout", type=Path, required=True,
                        help="the checkout the service runs from (absolute)")
    parser.add_argument("--env-file", type=Path, required=True,
                        help="the deployment's vinylgen.env, holding VINYLGEN_TUNNEL_HOST")
    parser.add_argument("--unit-dir", type=Path, required=True,
                        help="where the rendered units are installed")
    args = parser.parse_args(argv)
    host = env_value(args.env_file, "VINYLGEN_TUNNEL_HOST")
    if not host:
        print(f"error: VINYLGEN_TUNNEL_HOST is not set in {args.env_file}; set it to the SSH "
              "destination of the edge host", file=sys.stderr)
        return 1
    try:
        written = install(args.checkout, host, args.unit_dir)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for name, path in written.items():
        print(f"installed {path} (rendered for {args.checkout}"
              f"{', tunnel host ' + host if name == TUNNEL_UNIT else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
