# The stroke seam

S13's seam array of the tracer's stroke-trace card (`generations/tube/tests/data/stroke_seam.npy`, sha256
`b6835e5b06ea6e38ab3b9cf7ab8a653cce3b8af1abcafdb4d9b3725b914e608c`) as a vinylgen subject: 200 x 200, Paper (0) the
page, Disc (1) a disc of radius 120 about (200, 0), Ink (2) a 6 px ring over the disc's edge, leaving the canvas at
the top and right edges. The fixture of `tests/test_stroke_settings.py`: the ink is traced as a line colour, so the
map holds one stroke (the ring, round-capped, width 5.963, two free ends) and the face edge between the paper and
the disc lies under it (`under`), its pieces the stroke's. Made by `make.py` from the tracer checkout with t3 build 30
imported by path (its output `make.log` beside it); the test pins each file's sha256.

- `labels.npy`: the array less 1, `int16`, labels 0 to 2.
- `meta.json`: the palette as the palette stage writes it (`index`, `name`, `hex`, `rgb`, `share_percent`), the
  seam array's sha256 and the line colour.
- `map.json`: the trace at the default setting with `line_labels` [2] and one worker, its header as t3api 0.30
  writes it (the setting `{"half": {...}, "A1": 22.5, "A2": 45.0, "L": 4.0, "line_labels": [2], "width_mode":
  "stroke"}`, the build `0.30.0-source`, `palette` and `names` from `meta.json`); 2 faces, 3 edges (1 circular arc,
  6 border pieces), 1 stroke, 0 stroke nodes, 0 dots, 0 violations.
- `faces.svg`: `t3api.faces_svg_from_planar_map(pm, palette)` as 0.30 writes it: the two layers (the ink has no
  face), then the stroke group `<g id="strokes" fill="none">` as the root's last child with the one stroke path.
