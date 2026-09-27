"""Fetch the pinned icon subset of the icon fidelity corpus into ``tests/icons/``.

Each set is fetched from one pinned upstream commit (raw.githubusercontent.com),
with its licence file, and ``tests/icons/<set>/SOURCE.json`` records the
repository, commit, licence, path template, styles and glyph names that
``vinylgen.fidelity.icons`` reads. Glyphs are written as
``tests/icons/<set>/<style>/<name>.svg``, byte for byte as upstream has them.
A style file byte-identical to an earlier style's file of the same glyph (Material
Symbols Outlined often equals Sharp) is written once, under the earlier style;
``SOURCE.json``'s ``identical_styles`` maps ``name -> {style: earlier style}``.

Material Symbols takes 40 names whose three styles all differ upstream (the first
40 of its list) and 20 curve- and corner-rich names whose Outlined file equals
Sharp byte for byte (the last 20). The names were chosen for a spread of sharp corners, curves, holes and mixed
shapes, excluding glyphs the importer refuses (subpaths that touch or cross,
self-touching subpaths) and, for Material Symbols, glyphs whose three styles are
not all different.

Remix Icon is pinned to the last commit before its January 2026 relicensing
(``f526a92``, "Remix Icon License v1.0"); that parent commit is Apache 2.0.

Usage: fetch_icons.py [--dest DIR] [--set NAME ...]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "tests" / "icons"
RAW = "https://raw.githubusercontent.com/{repo}/{commit}/{path}"

SETS = {
    "material": {
        "title": "Material Symbols",
        "repo": "google/material-design-icons",
        "commit": "bd8cb85bd4bad964fe6918f79665bb40c3a8efef",
        "licence": "Apache-2.0",
        "licence_path": "LICENSE",
        "path": "symbols/web/{name}/materialsymbols{style}/{name}_24px.svg",
        "styles": ["sharp", "rounded", "outlined"],
        "names": [
            "delete", "bookmark", "mail", "photo_camera", "build", "shopping_cart",
            "local_cafe", "directions_car", "headphones", "notifications", "map",
            "emoji_events", "cake", "sports_esports", "save", "terminal", "chat", "forum",
            "wifi", "download", "image", "movie", "videocam", "castle", "fort", "church",
            "mosque", "temple_buddhist", "satellite_alt", "public_off", "backpack", "bed",
            "blender", "chess_rook", "coffee_maker", "iron", "liquor", "local_hospital",
            "lunch_dining", "stroller", "star", "favorite", "pets", "eco", "bug_report",
            "skull", "raven", "flutter_dash", "cyclone", "settings", "anchor",
            "local_fire_department", "hub", "sailing", "rocket_launch", "bolt", "content_cut",
            "crown", "swords", "science",
        ],
    },
    "bootstrap": {
        "title": "Bootstrap Icons",
        "repo": "twbs/icons",
        "commit": "6945b7006285d444cc17ff2e22c7691719229526",
        "licence": "MIT",
        "licence_path": "LICENSE",
        "path": "icons/{name}.svg",
        "styles": ["regular"],
        "names": [
            "gear-fill", "star-fill", "heart-fill", "bell-fill", "bookmark-star-fill",
            "puzzle-fill", "key-fill", "shield-fill-check", "rocket-takeoff-fill",
            "cup-hot-fill", "fire", "moon-stars-fill", "sun-fill", "suit-club-fill",
            "suit-spade-fill", "airplane-fill", "trophy-fill", "camera-fill",
            "emoji-smile-fill", "scissors",
        ],
    },
    "remix": {
        "title": "Remix Icon (fill)",
        "repo": "Remix-Design/RemixIcon",
        "commit": "f51ba97038d814f8a4138b4ad8379b447d591756",
        "licence": "Apache-2.0",
        "licence_path": "License",
        "path": "icons/{category}/{name}-fill.svg",
        "styles": ["fill"],
        "names": [
            "star", "heart-3", "settings-3", "bug", "rocket-2", "plane", "leaf", "fire",
            "moon-clear", "thunderstorms", "anchor", "key-2", "shield-star", "trophy",
            "vip-crown-2", "ghost", "bear-smile", "puzzle-2", "scissors-cut", "quill-pen",
        ],
        "categories": {
            "star": "System",
            "heart-3": "Health & Medical",
            "settings-3": "System",
            "bug": "Development",
            "rocket-2": "Map",
            "plane": "Map",
            "leaf": "Others",
            "fire": "Weather",
            "moon-clear": "Weather",
            "thunderstorms": "Weather",
            "anchor": "Map",
            "key-2": "Others",
            "shield-star": "System",
            "trophy": "Finance",
            "vip-crown-2": "Finance",
            "ghost": "User & Faces",
            "bear-smile": "User & Faces",
            "puzzle-2": "Development",
            "scissors-cut": "Design",
            "quill-pen": "Design",
        },
    },
    "heroicons": {
        "title": "Heroicons (solid, 24 px)",
        "repo": "tailwindlabs/heroicons",
        "commit": "616b7a4dbbf3d011760af8066262cd5c6b3868f3",
        "licence": "MIT",
        "licence_path": "LICENSE",
        "path": "optimized/24/solid/{name}.svg",
        "styles": ["solid"],
        "names": [
            "cog-6-tooth", "star", "heart", "bell", "bolt", "bug-ant", "cake", "camera",
            "fire", "key", "lock-closed", "moon", "puzzle-piece", "rocket-launch",
            "shield-check", "sparkles", "trophy", "face-smile", "finger-print", "scissors",
        ],
    },
}


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def glyph_path(spec: dict, style: str, name: str) -> str:
    category = spec.get("categories", {}).get(name, "")
    return urllib.parse.quote(spec["path"].format(name=name, style=style, category=category))


def fetch_set(set_name: str, spec: dict, dest: Path, names=None) -> dict:
    """Fetch one set's glyphs and licence into ``dest / set_name``; returns the
    SOURCE.json record."""
    names = list(spec["names"] if names is None else names)
    target = dest / set_name
    if target.exists():
        shutil.rmtree(target)
    jobs = [(style, name) for style in spec["styles"] for name in names]

    def fetch(job):
        style, name = job
        url = RAW.format(repo=spec["repo"], commit=spec["commit"],
                         path=glyph_path(spec, style, name))
        return job, _get(url)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(fetch, jobs))
    fetched = dict(results)
    identical: dict[str, dict[str, str]] = {}
    for name in names:
        for index, style in enumerate(spec["styles"]):
            data = fetched[(style, name)]
            earlier = next((other for other in spec["styles"][:index]
                            if fetched[(other, name)] == data), None)
            if earlier is not None:
                identical.setdefault(name, {})[style] = earlier
                continue
            path = target / style / f"{name}.svg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    licence = _get(RAW.format(repo=spec["repo"], commit=spec["commit"],
                              path=spec["licence_path"]))
    (target / "LICENSE").write_bytes(licence)
    record = {"set": set_name, "title": spec["title"],
              "repository": f"https://github.com/{spec['repo']}", "commit": spec["commit"],
              "licence": spec["licence"], "licence_file": "LICENSE",
              "upstream_licence_path": spec["licence_path"], "path_template": spec["path"],
              "styles": list(spec["styles"]), "names": names}
    if identical:
        record["identical_styles"] = identical
    if spec.get("categories"):
        record["categories"] = {name: spec["categories"][name] for name in names}
    (target / "SOURCE.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dest", type=Path, default=DEST)
    parser.add_argument("--set", action="append", choices=sorted(SETS), dest="sets")
    args = parser.parse_args(argv)
    for set_name in args.sets or sorted(SETS):
        record = fetch_set(set_name, SETS[set_name], args.dest)
        print(f"{set_name}: {len(record['names'])} names x {len(record['styles'])} styles "
              f"@ {record['commit'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
