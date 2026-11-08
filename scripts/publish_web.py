"""Publish a built web client as the page the service serves, by an atomic swap of the served directory.

The service serves ``web/live`` (``WebConfig.frontend_dir``), a symlink to one release under
``web/releases/``. A build (``npm --prefix web run build``, a gate, a dev build) writes ``web/dist``,
which the service never serves, so building never changes the live page. Only ``just deploy`` runs this,
after its own checks and with no job active:

1. the build is copied whole into a new release directory, ``web/releases/<UTC stamp>``, so a later build
   cannot change the files it serves;
2. a new symlink to it is made beside ``web/live`` and renamed over it (``os.replace``, one ``rename(2)``),
   so a request sees either the old page or the new one, never a mix;
3. every release but the new one and the one it replaced is removed (only directories under
   ``web/releases/`` whose names are release stamps).

A build without ``index.html`` is refused and nothing changes. ``web/live`` that exists and is not a
symlink is refused too: it would not be swapped atomically.

Run: .venv/bin/python scripts/publish_web.py --dist .tmp/deploy-web --web web
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

STAMP = re.compile(r"\d{8}-\d{6}-\d{6}")


def publish(dist: Path, web: Path, now: datetime | None = None) -> Path:
    """Copy ``dist`` into a new release under ``web/releases`` and point ``web/live`` at it atomically;
    prune every other release but the one it replaced. Returns the new release directory."""
    if not (dist / "index.html").is_file():
        raise SystemExit(f"refusing to publish {dist}: it holds no index.html")
    live = web / "live"
    if live.exists() and not live.is_symlink():
        raise SystemExit(f"refusing to publish: {live} is not a symlink, so it cannot be swapped atomically")
    releases = web / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    name = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S-%f")
    release = releases / name
    if release.exists():
        raise SystemExit(f"refusing to publish: {release} exists")
    previous = os.readlink(live) if live.is_symlink() else None
    shutil.copytree(dist, release)
    staged = web / ".live.new"
    if staged.is_symlink() or staged.exists():
        staged.unlink()
    staged.symlink_to(Path("releases") / name)
    os.replace(staged, live)
    keep = {name, Path(previous).name if previous else None}
    for child in releases.iterdir():
        if child.is_dir() and not child.is_symlink() and STAMP.fullmatch(child.name) and child.name not in keep:
            shutil.rmtree(child)
    return release


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, required=True, help="the built web client (holds index.html)")
    parser.add_argument("--web", type=Path, required=True, help="the web directory holding live and releases")
    args = parser.parse_args(argv)
    release = publish(args.dist, args.web)
    print(f"published {args.web / 'live'} -> {release}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
