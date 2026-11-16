"""Write the stroke-seam fixture: S13's seam array as a vinylgen subject's labels, its planar map traced by t3 build
30 with the ink colour as a line colour, and the faces file t3api 0.30 writes from it (the stroke group included).
The fixture of ``tests/test_stroke_settings.py`` (the stroke trace on vinylgen's trace step).

t3api 0.30, the release that carries the stroke trace (tracer card ``generations/tube/cards/stroke-trace/card.md``,
decision 7), does not exist yet, so this runs from the tracer checkout with t3 build 30 imported by path through the
tracer's ``make_expected.py`` of its packaging round 11
(``generations/tube/cards/turbo-tube-tiler/packaging/probes/round-11/make_expected.py``: the array ``seam_array``, the
import ``import_build30``, the document ``trace_doc``):

    cd /home/mat/projects/tracer && .venv/bin/python /home/mat/projects/vinylgen/tests/fixtures/stroke-seam/make.py

The array is S13's (``generations/tube/tests/data/stroke_seam.npy``, sha256 pinned by ``make_expected``): 200 x 200,
label 1 the paper, label 2 a disc of radius 120 about (200, 0), label 3 a 6 px ring over the disc's edge, leaving the
canvas at the top and right edges. vinylgen's labels are palette indices from 0, so the fixture's ``labels.npy`` is
the array less 1 (``int16``): Paper 0, Disc 1, Ink 2; ``meta.json`` holds the palette as the palette stage writes it
(``index``, ``name``, ``hex``, ``rgb``, ``share_percent``). The ink is the line colour: ``line_labels`` [2].

``map.json`` is the trace at the default setting with ``line_labels=[2]``, one worker, after ``t3.setup()`` and the
warm-up at the default as ``t3api._t3`` does, its header as t3api 0.30 writes it: ``setting`` the canonical form
``{"half": {...}, "A1": 22.5, "A2": 45.0, "L": 4.0, "line_labels": [2], "width_mode": "stroke"}``, ``build``
"0.30.0-source", ``palette`` and ``names`` from ``meta.json`` as ``t3api.planar_map(result, palette)`` writes them,
then ``nodes``, ``edges``, ``faces``, ``strokes``, ``stroke_nodes``, ``dots``, ``not_stroke``.

``faces.svg`` is ``t3api.faces_svg_from_planar_map(pm, palette)`` as 0.30 must write it: build 30's
``faces_svg_from_map`` (the layers, byte for byte 0.29.1's) with the stroke group the card decides as the root's last
child: ``<g id="strokes" fill="none">``, one ``<path d=... stroke=... stroke-width=... stroke-linecap=...
stroke-linejoin=.../>`` per stroke in the map's order (the ``d`` the pieces' ``svg_d`` in order, each moveto after the
first dropped, ``Z`` closing a cyclic stroke; the colour the label's hex; the width as ``num``; round and round for
the ``round`` style, butt and miter for ``butt``), then one ``<path d="M x y l0 0" stroke=... stroke-width=...
stroke-linecap="round"/>`` per dot, then ``</g>``, each on its own line.

Printed: one RESULT line per file (its bytes, sha256 and counts); the test pins the shas.
"""
import hashlib
import importlib.util
import json
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TRACER = "/home/mat/projects/tracer"
ROUND_11 = f"{TRACER}/generations/tube/cards/turbo-tube-tiler/packaging/probes/round-11"
BUILD_ID = "0.30.0-source"
LINE_COLOUR = 2
PALETTE = [{"index": 0, "name": "Paper", "hex": "#F4E6AD"}, {"index": 1, "name": "Disc", "hex": "#E4B33A"},
           {"index": 2, "name": "Ink", "hex": "#050301"}]


def make_expected():
    spec = importlib.util.spec_from_file_location("make_expected_round_11", f"{ROUND_11}/make_expected.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rgb(hex_: str) -> list:
    h = hex_.lstrip("#")
    return [int(h[i: i + 2], 16) for i in (0, 2, 4)]


def palette_rows(labels: np.ndarray) -> list:
    """``meta.json``'s palette: each colour's index, name, hex, rgb and share of the pixels, in percent."""
    total = labels.size
    return [{**row, "rgb": rgb(row["hex"]),
             "share_percent": round(100.0 * float(np.count_nonzero(labels == row["index"])) / total, 4)}
            for row in PALETTE]


def palette_fields(doc: dict) -> tuple:
    """(palette, names) as ``t3api._palette_rgb`` writes them for the map's labels and the palette's indices."""
    pal = {p["index"]: p for p in PALETTE}
    labels = {f["label"] for f in doc["faces"]} | {S["label"] for S in doc.get("strokes", [])}
    n = max(list(pal) + list(labels)) + 1
    return ([rgb(pal[i]["hex"]) if i in pal else None for i in range(n)],
            [pal[i]["name"] if i in pal else None for i in range(n)])


def stroke_d(t3, S: dict) -> str:
    out = []
    for k, p in enumerate(S["pieces"]):
        d = t3.svg_d(list(p["seg"]))
        out.append(d if k == 0 else d[next(i for i, ch in enumerate(d) if ch in "LCA"):])
    return "".join(out) + ("Z" if S["cyclic"] else "")


def stroke_group(t3, doc: dict) -> list:
    hexes = {p["index"]: p["hex"] for p in PALETTE}
    out = ['<g id="strokes" fill="none">']
    for S in doc["strokes"]:
        cap, join = ("round", "round") if S["style"] == "round" else ("butt", "miter")
        out.append(f'<path d="{stroke_d(t3, S)}" stroke="{hexes[S["label"]]}" stroke-width="{t3.num(S["width"])}" '
                   f'stroke-linecap="{cap}" stroke-linejoin="{join}"/>')
    for D in doc["dots"]:
        out.append(f'<path d="M{t3.num(D["c"][0])} {t3.num(D["c"][1])}l0 0" stroke="{hexes[D["label"]]}" '
                   f'stroke-width="{t3.num(D["width"])}" stroke-linecap="round"/>')
    out.append("</g>")
    return out


def faces_svg(t3, doc: dict) -> str:
    faces = t3.planar_map_faces(doc)
    svg = t3.faces_svg_from_map(faces, doc["W"], doc["H"], PALETTE)
    lines = svg.rstrip("\n").split("\n")
    assert lines[-1] == "</svg>", lines[-1]
    return "\n".join(lines[:-1] + stroke_group(t3, doc) + ["</svg>"]) + "\n"


def result(name: str, data: bytes, extra: dict) -> None:
    print("RESULT", name, json.dumps({"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), **extra},
                                     sort_keys=True))


if __name__ == "__main__":
    me = make_expected()
    t3 = me.import_build30()
    labels = np.ascontiguousarray((me.seam_array().astype(np.int16) - 1))
    assert labels.min() == 0 and labels.max() == LINE_COLOUR, (labels.min(), labels.max())
    np.save(os.path.join(HERE, "labels.npy"), labels)
    result("labels.npy", labels.tobytes(), {"shape": list(labels.shape), "dtype": str(labels.dtype),
                                           "note": "the sha256 is of the array's bytes, not the .npy file's"})
    meta = {"source": "S13's seam array, generations/tube/tests/data/stroke_seam.npy, less 1",
            "seam_sha256": me.SEAM_SHA, "line_colour": LINE_COLOUR, "palette": palette_rows(labels)}
    meta_text = json.dumps(meta, indent=1) + "\n"
    with open(os.path.join(HERE, "meta.json"), "w") as f:
        f.write(meta_text)
    result("meta.json", meta_text.encode(), {"palette": [(r["index"], r["name"], r["share_percent"])
                                                        for r in meta["palette"]]})
    doc = me.trace_doc(t3, labels, {"line_labels": [LINE_COLOUR]}, build=BUILD_ID)
    doc["palette"], doc["names"] = palette_fields(doc)
    chk = t3.planar_map_check(doc)
    text = t3.planar_map_text(doc)
    with open(os.path.join(HERE, "map.json"), "w") as f:
        f.write(text)
    result("map.json", text.encode(), {**me.counts_of(text), "violations": chk["violations"],
                                       "problems": chk.get("problems"), "header": text[:text.index('"nodes"')],
                                       "strokes_meta": [{k: S[k] for k in ("label", "cyclic", "width", "style", "ends",
                                                                            "flags")} | {"pieces": len(S["pieces"])}
                                                        for S in doc["strokes"]]})
    svg = faces_svg(t3, doc)
    with open(os.path.join(HERE, "faces.svg"), "w") as f:
        f.write(svg)
    result("faces.svg", svg.encode(), {"lines": svg.count("\n"),
                                       "stroke_paths": sum(1 for ln in svg.split("\n") if "stroke-linecap" in ln)})
