"""Card 3 of vinylgen-on-t3: validation and the semantic audit under t3, and ``trace.json``.

The contract is the tracer card ``cards/vinylgen-on-t3/card.md`` section 4 (and section 5's three
soft node limits). Every test traces the test array (``tests/fixtures/t3-test-array``) through the
code a user reaches, as card 2's tests do (``tests/test_t3_trace.py``, whose fixture helpers this
module reuses): ``trace_subject`` on a subject folder built the way the palette stage writes one,
the trace CLI's ``main``, or the web pipeline's trace, validate and export jobs. Nothing mocks a t3
result. The two loud-failure tests corrupt the planar map the real trace made (a reversed
reference, a moved node) before the trace writes and checks it, so the failing branch of the
trace's own check is the one reached.

The audit's tolerances are the setting's: ``maximum_deviation = t3api.SETTING["w"] / 2`` and
``minimum_feature_span = feature_span(maximum_deviation)``.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import copy
import hashlib
import io
import json
import re
import tempfile
import time
import unittest
import zipfile
from dataclasses import asdict
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image
from scipy import ndimage

import numba
import scipy
import t3api

from test_t3_trace import (
    FIXTURE,
    SCRATCH,
    TRACE_BOUND_S,
    Recorder,
    _Api,
    build_subject,
    fixture_palette,
)
from vinylgen.contracts import CleanupSettings, SegmentationBundle, TraceSettings
from vinylgen.cutter_cleanup import resolve_physical_size
from vinylgen.preview import preview_plan
from vinylgen.tracing import MISSED_DETAIL_RGBA, feature_span, trace_subject
from vinylgen.validation import validate_semantic_preservation, validate_svg

# t3's tube half-width, the audit's maximum deviation (card section 4).
TUBE_HALF_WIDTH = float(t3api.SETTING["w"]) / 2.0
MISSED_OVERLAY = "trace-missed-details-overlay.png"
SEMANTIC_INSPECTION = "trace-semantic-warnings.png"
FOUR = ndimage.generate_binary_structure(2, 1)
# Card section 4's removed keys.
REMOVED_TOP_LEVEL = ("lengths", "topology", "raster_partition_validation", "fit_metrics",
                     "layer_order")
# The planar map's counts ``planar_map_check`` reports.
CHECK_COUNTS = ("nodes", "edges", "border_edges", "drawn_null_sides", "faces", "rings",
                "references", "pieces", "violations")


def trace(subject: Path, settings: TraceSettings | None = None, *,
          planar_map=None, **kwargs) -> dict:
    """``trace_subject`` on ``subject`` with ``t3api.trace`` recorded. ``planar_map``, when given,
    is wrapped around the real ``t3api.planar_map`` (to corrupt the document the trace writes).
    An exception is kept, not raised, so each test reports its own failure."""
    recorder = Recorder()
    outcome = {"recorder": recorder, "subject": subject, "error": None, "audit": None}
    patches = [mock.patch.object(t3api, "trace", recorder)]
    if planar_map is not None:
        real = t3api.planar_map
        patches.append(mock.patch.object(
            t3api, "planar_map", lambda *a, **k: planar_map(real(*a, **k))))
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        try:
            outcome["audit"] = trace_subject(subject, settings or TraceSettings(), {}, **kwargs)
        except Exception as error:  # noqa: BLE001 - re-raised by the test that needs the trace
            outcome["error"] = error
    return outcome


def raise_error(outcome: dict) -> dict:
    if outcome["error"] is not None:
        raise outcome["error"]
    return outcome


def read_audit(subject: Path) -> dict:
    return json.loads((subject / "trace.json").read_text())


def read_map(subject: Path) -> dict:
    return t3api.read_planar_map((subject / "map.json").read_text())


def component_at(labels: np.ndarray, x: int, y: int) -> np.ndarray:
    """The four-connected colour region holding centre (x, y): what an explicit component
    protection selects (vinylgen GLOSSARY, "explicit component protection")."""
    regions, _ = ndimage.label(labels == labels[y, x], structure=FOUR)
    return regions == regions[y, x]


def bounds_pattern(mask: np.ndarray) -> re.Pattern:
    """The component's pixel bounds ``x0, y0, x1, y1`` (the audit's ``source_bounds_px``), in
    that order, whatever separates them."""
    ys, xs = np.nonzero(mask)
    x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    return re.compile(rf"(?<!\d){x0}\D+{y0}\D+{x1}\D+{y1}(?!\d)")


def face_node_count(path: dict) -> int:
    """Inkscape's node count of a face path: its node types less one per closed subpath."""
    return len(path["nodetypes"]) - path["d"].count("M")


def expected_layer_stats(pm: dict, labels: np.ndarray, palette: list[dict],
                         included: list[int]) -> list[dict]:
    """Card section 2's ``geometry.edge`` stats, counted from ``map.json`` independently of the
    writer: paths, nodes, holes, node types, pieces by kind, 4-connected source components."""
    paths = {row["face"]: row for row in t3api.planar_map_paths(pm)}
    layers = []
    for index in included:
        faces = [face for face in pm["faces"] if face["label"] == index and face["face"] in paths]
        elements: collections.Counter = collections.Counter()
        node_types: collections.Counter = collections.Counter()
        for face in faces:
            node_types.update(paths[face["face"]]["nodetypes"])
            for ring in face["rings"]:
                for reference in ring:
                    for piece in pm["edges"][reference if reference >= 0 else ~reference]["pieces"]:
                        if piece["kind"] == "A":
                            seg = piece["seg"]
                            elements["A circular" if seg[3] == seg[4] else "A elliptical"] += 1
                        elif piece["kind"] in ("L", "C"):
                            elements[piece["kind"]] += 1
        layers.append({
            "index": index, "name": palette[index]["name"], "hex": palette[index]["hex"],
            "paths": len(faces),
            "nodes": sum(face_node_count(paths[face["face"]]) for face in faces),
            "holes": sum(len(face["rings"]) - 1 for face in faces),
            "node_types": dict(node_types), "elements": dict(elements),
            "source_colour_components": int(ndimage.label(labels == index, structure=FOUR)[1]),
            "components": sorted((paths[face["face"]]["d"], face_node_count(paths[face["face"]]),
                                  len(face["rings"]) - 1) for face in faces),
        })
    return layers


def svg_path_d(subject: Path) -> dict[str, str]:
    from vinylgen.svg_writer import SVG
    import xml.etree.ElementTree as ET

    root = ET.parse(subject / "traced-edge.svg").getroot()
    return {node.attrib["id"]: node.attrib["d"] for node in root.iter(f"{{{SVG}}}path")}


class _Traced(unittest.TestCase):
    """One trace of the test array with every colour included and the default settings."""

    @classmethod
    def setUpClass(cls) -> None:
        SCRATCH.mkdir(exist_ok=True)
        cls._temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        cls.outcome = trace(build_subject(Path(cls._temp.name)))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def traced(self) -> tuple[Path, dict, object]:
        """The subject, its trace.json and the t3 result of its one call."""
        raise_error(self.outcome)
        return (self.outcome["subject"], read_audit(self.outcome["subject"]),
                self.outcome["recorder"].results[0])


class TraceJsonTest(_Traced):
    """``trace.json`` as card section 4 lists it."""

    def test_sources_name_every_input_and_the_planar_map(self):
        subject, audit, _ = self.traced()
        self.assertEqual(audit["sources"], {
            "original": str(subject / "source.png"),
            "flattened": str(subject / "flattened.png"),
            "palette": str(subject / "palette.json"),
            "segmentation": str(subject / "segmentation.npz"),
            "map": str(subject / "map.json")})

    def test_the_frame_settings_and_colours_keep_their_meaning(self):
        """Pins what card 2 built (passes on 69d6073): subject, raster dimensions, mode, physical
        size, settings, cleanup settings, colours and cleanup as before t3."""
        subject, audit, _ = self.traced()
        labels = np.load(FIXTURE / "labels.npy")
        height, width = labels.shape
        palette = json.loads((subject / "palette.json").read_text())["palette"]
        self.assertEqual(audit["subject"], subject.name)
        self.assertEqual(audit["raster_dimensions"], [width, height])
        self.assertEqual(audit["mode"], "pixels")
        self.assertIsNone(audit["physical_dimensions_mm"])
        self.assertEqual(audit["settings"], json.loads(json.dumps(asdict(TraceSettings()))))
        self.assertEqual(audit["cleanup_settings"], json.loads(json.dumps(
            asdict(resolve_physical_size(CleanupSettings(), width, height)))))
        self.assertEqual(audit["colours"], {"included": palette, "omitted": None})
        self.assertIsNone(audit["cleanup"])

    def test_geometry_holds_section_2s_edge_stats(self):
        """Pins what card 2 built (passes on 69d6073): ``geometry`` is ``edge`` only, one row per
        included colour in palette order, its counts those of ``map.json``."""
        subject, audit, _ = self.traced()
        self.assertEqual(set(audit["geometry"]), {"edge"})
        pm = read_map(subject)
        palette = json.loads((subject / "palette.json").read_text())["palette"]
        expected = expected_layer_stats(pm, np.load(FIXTURE / "labels.npy"), palette, [0, 1, 2, 3])
        d_by_id = svg_path_d(subject)
        actual = []
        for layer in audit["geometry"]["edge"]:
            self.assertEqual(layer["construction"], "t3 planar map")
            actual.append({
                **{key: layer[key] for key in ("index", "name", "hex", "paths", "nodes", "holes",
                                               "node_types", "source_colour_components")},
                "elements": {key: value for key, value in layer["elements"].items()
                             if key in ("L", "A circular", "A elliptical", "C") and value},
                "components": sorted((d_by_id[row["id"]], row["nodes"], row["holes"])
                                     for row in layer["components"]),
            })
        self.assertEqual(actual, expected)

    def test_determinism_and_preview_keep_their_meaning(self):
        """Pins what card 2 built (passes on 69d6073): the input and ``map.json`` digests and
        the preview plan of ``traced-edge.svg``."""
        subject, audit, _ = self.traced()
        self.assertEqual(audit["determinism"], {
            "input_sha256": hashlib.sha256(
                (subject / "flattened.png").read_bytes() + (subject / "palette.json").read_bytes()
                + (subject / "segmentation.npz").read_bytes()).hexdigest(),
            "map_sha256": hashlib.sha256((subject / "map.json").read_bytes()).hexdigest()})
        plan = preview_plan(subject / "traced-edge.svg")
        self.assertEqual(audit["preview"], json.loads(json.dumps(
            {"file": "traced.png", **{k: v for k, v in plan.items() if k != "arguments"}})))

    def test_the_removed_keys_are_absent(self):
        """Pins what card 2 built (passes on 69d6073): ``lengths``, ``topology``,
        ``raster_partition_validation``, ``fit_metrics`` (anywhere), ``layer_order``,
        ``geometry.overlap`` and ``validation.overlap`` are not written."""
        subject, audit, _ = self.traced()
        for key in REMOVED_TOP_LEVEL:
            self.assertNotIn(key, audit)
        self.assertNotIn("overlap", audit["geometry"])
        self.assertNotIn("overlap", audit["validation"])
        self.assertNotIn("fit_metrics", (subject / "trace.json").read_text())

    def test_backend_records_the_build_its_setting_and_the_libraries(self):
        _, audit, _ = self.traced()
        self.assertEqual(audit["backend"], {
            "tracer": "t3api", "build": t3api.BUILD_ID, "version": t3api.VERSION,
            "setting": t3api.SETTING, "numpy": np.__version__, "scipy": scipy.__version__,
            "numba": numba.__version__})

    def test_t3_holds_the_result_summary_timings_and_gap_connectors(self):
        subject, audit, result = self.traced()
        stats: dict = {}
        t3api.faces_svg_from_planar_map(read_map(subject), fixture_palette(), stats)
        self.assertEqual(audit["t3"], {
            "summary": json.loads(json.dumps(result.summary)), "seconds": result.seconds,
            "warm_s": result.warm_s, "workers": result.workers,
            "gaps": stats["gaps"], "gap_max": stats["gap_max"]})

    def test_validation_edge_records_validate_svg_planar_map_check_and_read_back(self):
        subject, audit, result = self.traced()
        palette = json.loads((subject / "palette.json").read_text())["palette"]
        edge = audit["validation"]["edge"]
        expected_svg = validate_svg(subject / "traced-edge.svg", palette, [0, 1, 2, 3])
        self.assertTrue(expected_svg["passed"])
        self.assertEqual({key: edge.get(key) for key in expected_svg},
                         json.loads(json.dumps(expected_svg)))
        pm = read_map(subject)
        check = t3api.planar_map_check(pm)
        self.assertEqual(check["violations"], 0)
        recorded = edge["planar_map_check"]
        self.assertEqual({key: recorded.get(key) for key in CHECK_COUNTS},
                         {key: check[key] for key in CHECK_COUNTS})
        self.assertFalse(recorded.get("problems"), "a passing map records no problem")
        self.assertEqual(int(np.count_nonzero(
            t3api.paint_planar_map(pm) != np.asarray(result.painted))), 0)
        self.assertEqual(edge["read_back"]["differ"], 0)

    def test_a_clean_trace_warns_of_nothing(self):
        """Pins what card 2 built (passes on 69d6073): the test array with no protection, at the
        default limits, has no gap connector (t3 ``gaps`` 0), passes the audit and warns of
        nothing; no inspection overlay is left."""
        subject, audit, _ = self.traced()
        stats: dict = {}
        t3api.faces_svg_from_planar_map(read_map(subject), fixture_palette(), stats)
        self.assertEqual(stats["gaps"], 0)
        self.assertTrue(audit["validation"]["semantic"]["passed"])
        self.assertEqual(audit["warnings"], [])
        self.assertFalse((subject / MISSED_OVERLAY).exists())
        self.assertFalse((subject / SEMANTIC_INSPECTION).exists())


class SemanticAuditTest(unittest.TestCase):
    """The semantic audit at t3's tube half-width, read on ``result.painted`` with the omitted
    colour's centres included, and no Inkscape render (card section 4)."""

    OMITTED = 0  # Black: dark, so the audit's dark-support reading sees whether it is kept

    @classmethod
    def setUpClass(cls) -> None:
        SCRATCH.mkdir(exist_ok=True)
        cls._temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        cls.outcome = trace(build_subject(Path(cls._temp.name)), removed_index=cls.OMITTED)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def test_the_audit_reads_the_painted_labels_at_the_tube_half_width(self):
        """Pins what card 2 built (passes on 69d6073)."""
        subject = raise_error(self.outcome)["subject"]
        audit = read_audit(subject)
        painted = np.asarray(self.outcome["recorder"].results[0].painted)
        self.assertGreaterEqual(int(painted.min()), 0, "the test array paints every centre")
        self.assertIn(self.OMITTED, np.unique(painted).tolist())
        segmentation = SegmentationBundle.load(subject / "segmentation.npz")
        original = np.asarray(Image.open(subject / "source.png").convert("RGB"))
        flattened = np.asarray(Image.open(subject / "flattened.png").convert("RGB"))
        expected = validate_semantic_preservation(
            original, flattened, vector_rgb=segmentation.palette_rgb[painted],
            protected_features=segmentation.protected_features,
            segmentation_labels=segmentation.labels, vector_labels=painted.astype(np.int16),
            maximum_deviation=TUBE_HALF_WIDTH,
            minimum_feature_span=feature_span(TUBE_HALF_WIDTH))
        expected = json.loads(json.dumps(expected))
        semantic = audit["validation"]["semantic"]
        self.assertEqual(semantic["settings"]["maximum_deviation_px"], TUBE_HALF_WIDTH)
        self.assertEqual(semantic["settings"]["minimum_feature_span_px"],
                         feature_span(TUBE_HALF_WIDTH))
        self.assertEqual({key: semantic.get(key) for key in expected}, expected)


class ExplicitProtectionTest(unittest.TestCase):
    """A changed explicit protected component is a warning naming it, its share of changed
    centres and the missed-detail overlay; export is not blocked (card section 4).

    Three explicit components of the test array, each the four-connected colour region beneath a
    selected point, found from the audit's own output on a trace of the test array: two 1 to 3 px
    wide features the trace changes (Golden Yellow's single pixel at (0, 136), 1 of 1 centre
    changed, and Black's 9-pixel speck at (98, 396), 3 of 9) and one it keeps (Golden Yellow's
    11 pixels at (399, 276), 0 of 11). The single pixel fails the audit's protected-feature check,
    so the audit fails too."""

    CHANGED = ((0, 136), (98, 396))
    KEPT = ((399, 276),)

    @classmethod
    def protect(cls, subject: Path) -> None:
        segmentation = SegmentationBundle.load(subject / "segmentation.npz")
        mask = np.zeros(segmentation.labels.shape, dtype=bool)
        for x, y in cls.CHANGED + cls.KEPT:
            mask |= component_at(segmentation.labels, x, y)
        segmentation.explicit_protected_features = mask
        segmentation.protected_features = mask.copy()
        segmentation.save(subject / "segmentation.npz")

    @classmethod
    def setUpClass(cls) -> None:
        SCRATCH.mkdir(exist_ok=True)
        cls._temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        subject = build_subject(Path(cls._temp.name))
        cls.protect(subject)
        cls.outcome = trace(subject)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def components(self) -> tuple[list[dict], list[dict]]:
        """The changed and the kept components with their counts on this trace."""
        labels = np.load(FIXTURE / "labels.npy")
        painted = np.asarray(self.outcome["recorder"].results[0].painted)
        palette = fixture_palette()

        def row(x: int, y: int) -> dict:
            mask = component_at(labels, x, y)
            changed = mask & (painted != labels)
            return {"mask": mask, "changed": changed, "size": int(mask.sum()),
                    "count": int(changed.sum()), "name": palette[int(labels[y, x])]["name"]}

        changed = [row(x, y) for x, y in self.CHANGED]
        kept = [row(x, y) for x, y in self.KEPT]
        for component in changed:
            self.assertGreater(component["count"], 0)
        for component in kept:
            self.assertEqual(component["count"], 0)
        return changed, kept

    def test_each_changed_explicit_component_is_named_with_its_share_and_the_trace_completes(self):
        subject = raise_error(self.outcome)["subject"]
        audit = read_audit(subject)
        self.assertTrue(audit["validation"]["edge"]["passed"], "the warning blocks nothing")
        changed, kept = self.components()
        for component in changed:
            share = re.compile(rf"(?<!\d){component['count']} of {component['size']}(?!\d)")
            pattern = bounds_pattern(component["mask"])
            naming = [w for w in audit["warnings"]
                      if pattern.search(w) and share.search(w) and component["name"] in w
                      and MISSED_OVERLAY in w]
            self.assertTrue(naming, (component["name"], pattern.pattern, audit["warnings"]))
        for component in kept:
            pattern = bounds_pattern(component["mask"])
            self.assertFalse([w for w in audit["warnings"] if pattern.search(w)],
                             "a kept explicit component is not named")

    def test_the_missed_detail_overlay_marks_every_changed_explicit_centre(self):
        subject = raise_error(self.outcome)["subject"]
        changed, _ = self.components()
        overlay = np.asarray(Image.open(subject / MISSED_OVERLAY).convert("RGBA"))
        self.assertEqual(overlay.shape[:2], changed[0]["mask"].shape)
        for component in changed:
            marked = overlay[component["changed"]]
            self.assertTrue(np.all(marked == np.asarray(MISSED_DETAIL_RGBA, dtype=np.uint8)),
                            (component["name"], marked.tolist()))

    def test_the_audits_failure_warns_with_its_overlay(self):
        """Pins what card 2 built (passes on 69d6073): the audit fails (the single pixel is
        drawn away), a warning says so and names its inspection image, and both images exist."""
        subject = raise_error(self.outcome)["subject"]
        audit = read_audit(subject)
        self.assertFalse(audit["validation"]["semantic"]["passed"])
        self.assertTrue([w for w in audit["warnings"] if "semantic" in w
                         and (SEMANTIC_INSPECTION in w or MISSED_OVERLAY in w)], audit["warnings"])
        self.assertTrue((subject / SEMANTIC_INSPECTION).exists())
        self.assertTrue((subject / MISSED_OVERLAY).exists())


class SoftNodeLimitTest(unittest.TestCase):
    """Section 5's three soft node limits, each a warning naming the limit and what exceeds it.

    The limits are set one under the test array's own largest path, largest layer and document,
    counted from ``paths.json`` (Inkscape's node count: node types less one per subpath), so the
    largest path, the largest layer and the document each exceed theirs by one node."""

    @classmethod
    def setUpClass(cls) -> None:
        SCRATCH.mkdir(exist_ok=True)
        cls._temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        paths = json.loads((FIXTURE / "paths.json").read_text())
        per_layer = collections.Counter()
        for path in paths:
            per_layer[path["label"]] += face_node_count(path)
        cls.limits = TraceSettings(
            soft_path_nodes=max(face_node_count(path) for path in paths) - 1,
            soft_layer_nodes=max(per_layer.values()) - 1,
            soft_document_nodes=sum(per_layer.values()) - 1)
        cls.outcome = trace(build_subject(Path(cls._temp.name)), cls.limits)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def audit(self) -> dict:
        return read_audit(raise_error(self.outcome)["subject"])

    def test_a_path_over_the_soft_path_node_limit_is_named(self):
        audit = self.audit()
        rows = [row for layer in audit["geometry"]["edge"] for row in layer["components"]]
        over = {row["id"] for row in rows if row["nodes"] > self.limits.soft_path_nodes}
        under = {row["id"] for row in rows} - over
        self.assertTrue(over)
        warnings = [w for w in audit["warnings"] if "path-node limit" in w]
        for path_id in over:
            self.assertTrue([w for w in warnings if re.search(rf"\b{path_id}\b", w)],
                            (path_id, audit["warnings"]))
        for path_id in under:
            self.assertFalse([w for w in warnings if re.search(rf"\b{path_id}\b", w)], path_id)

    def test_a_layer_over_the_soft_layer_node_limit_is_named(self):
        audit = self.audit()
        layers = audit["geometry"]["edge"]
        over = {layer["name"] for layer in layers if layer["nodes"] > self.limits.soft_layer_nodes}
        self.assertTrue(over)
        warnings = [w for w in audit["warnings"] if "layer-node limit" in w]
        for layer in layers:
            named = [w for w in warnings if layer["name"] in w]
            if layer["name"] in over:
                self.assertTrue(named, (layer["name"], audit["warnings"]))
            else:
                self.assertFalse(named, layer["name"])

    def test_the_document_over_the_soft_document_node_limit_warns(self):
        """Pins what card 2 built for this one limit (passes on 69d6073)."""
        audit = self.audit()
        self.assertGreater(sum(layer["nodes"] for layer in audit["geometry"]["edge"]),
                           self.limits.soft_document_nodes)
        self.assertTrue([w for w in audit["warnings"] if "document-node limit" in w],
                        audit["warnings"])


def reverse_a_reference(pm: dict) -> dict:
    """The planar map with its first face's first ring reference reversed."""
    pm = copy.deepcopy(pm)
    ring = pm["faces"][0]["rings"][0]
    ring[0] = ~ring[0]
    return pm


def move_a_node(pm: dict) -> dict:
    """The planar map with its first interior node met only by lines moved one pixel spacing
    along x and y, the lines' ends moved with it: every invariant of the file still holds, but
    the drawing no longer paints t3's labels."""
    pm = copy.deepcopy(pm)
    for index, (x, y) in enumerate(pm["nodes"]):
        pieces = [piece for edge in pm["edges"] for piece in edge["pieces"]
                  if [x, y] in (piece["p0"], piece["p1"])]
        if 0 < x < pm["W"] and 0 < y < pm["H"] and pieces and all(
                piece["seg"][0] == "L" for piece in pieces):
            break
    else:
        raise AssertionError("no interior node met only by lines")
    moved = [x + 1.0, y + 1.0]
    pm["nodes"][index] = moved
    for piece in pieces:
        if piece["p0"] == [x, y]:
            piece["p0"] = list(moved)
            piece["seg"][1:3] = moved
        if piece["p1"] == [x, y]:
            piece["p1"] = list(moved)
            piece["seg"][3:5] = moved
    return pm


class GateFailureTest(unittest.TestCase):
    """``planar_map_check`` and the read-back paint are the trace's gate: a violation or a
    differing centre fails the trace loudly, a raised error naming the count, and nothing is
    written as passed (card section 4)."""

    def setUp(self) -> None:
        SCRATCH.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def corrupted(self, corrupt, subject: Path | None = None) -> tuple[dict, dict]:
        """A trace whose planar map is ``corrupt`` of the real one, and that corrupted map."""
        made: list[dict] = []

        def record(pm: dict) -> dict:
            made.append(corrupt(pm))
            return made[-1]

        outcome = trace(subject or build_subject(self.root), planar_map=record)
        self.assertEqual(len(made), 1, outcome["error"])
        return outcome, made[0]

    def assert_failed_naming(self, outcome: dict, count: int) -> None:
        self.assertIsInstance(outcome["error"], RuntimeError, outcome["error"])
        self.assertRegex(str(outcome["error"]), rf"(?<!\d){count}(?!\d)")

    def test_a_reversed_reference_fails_the_trace_naming_the_violation_count(self):
        """Pins what card 2 built (passes on 69d6073) on a fresh subject folder."""
        outcome, pm = self.corrupted(reverse_a_reference)
        violations = t3api.planar_map_check(pm)["violations"]
        self.assertGreater(violations, 0)
        self.assert_failed_naming(outcome, violations)
        self.assertFalse((outcome["subject"] / "trace.json").exists())

    def test_a_moved_node_fails_the_trace_naming_the_differing_centres(self):
        """Pins what card 2 built (passes on 69d6073) on a fresh subject folder."""
        outcome, pm = self.corrupted(move_a_node)
        self.assertEqual(t3api.planar_map_check(pm)["violations"], 0,
                         "the file's invariants hold: only the read-back paint can see it")
        painted = np.asarray(outcome["recorder"].results[0].painted)
        differ = int(np.count_nonzero(t3api.paint_planar_map(pm) != painted))
        self.assertGreater(differ, 0)
        self.assert_failed_naming(outcome, differ)
        self.assertFalse((outcome["subject"] / "trace.json").exists())

    def test_a_failed_retrace_leaves_no_trace_json_passing_the_map_on_disk(self):
        """A folder traced once and passing, then traced again with a failing map: afterwards no
        ``trace.json`` in it vouches for a ``map.json`` other than the one it checked. The
        failing map is the moved node's, which card 2's gate already reaches."""
        subject = build_subject(self.root)
        raise_error(trace(subject))
        outcome, _ = self.corrupted(move_a_node, subject)
        self.assertIsInstance(outcome["error"], RuntimeError, outcome["error"])
        if (subject / "trace.json").exists():
            audit = read_audit(subject)
            self.assertEqual(audit["determinism"]["map_sha256"],
                             hashlib.sha256((subject / "map.json").read_bytes()).hexdigest(),
                             "trace.json passes a map.json other than the one on disk")
            painted = np.asarray(outcome["recorder"].results[0].painted)
            self.assertTrue(np.array_equal(t3api.paint_planar_map(read_map(subject)), painted),
                            "the map.json trace.json vouches for paints t3's labels")


class CliConfigTest(unittest.TestCase):
    """``cli_trace --config`` reads a new ``trace.json``: its ``settings`` with its
    ``cleanup_settings`` (card section 4's consumer census). Pins what card 2 built (passes on
    69d6073)."""

    OMITTED = "Burnt Orange"

    def test_a_trace_configured_from_a_trace_json_repeats_it(self):
        from vinylgen.cli_trace import main

        SCRATCH.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            root = Path(name)
            first = build_subject(root / "first")
            second = build_subject(root / "second")
            # A size and an omitted colour, so the cleanup settings carry something to repeat.
            width_mm = float(np.load(FIXTURE / "labels.npy").shape[1])
            argv_first = ["svg_trace.py", str(first), "--remove-colour", self.OMITTED,
                          "--width-mm", repr(width_mm)]
            argv_second = ["svg_trace.py", str(second), "--config", str(first / "trace.json")]
            for argv in (argv_first, argv_second):
                with mock.patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
                    main()
            one, two = read_audit(first), read_audit(second)
            self.assertEqual(one["mode"], "physical")
            self.assertEqual(one["colours"]["omitted"]["name"], self.OMITTED)
            for key in ("settings", "cleanup_settings", "mode", "physical_dimensions_mm",
                        "colours"):
                self.assertEqual(two[key], one[key], key)
            self.assertEqual((second / "map.json").read_bytes(), (first / "map.json").read_bytes())
            self.assertEqual((second / "traced-edge.svg").read_bytes(),
                             (first / "traced-edge.svg").read_bytes())


class WebValidateAndExportTest(unittest.IsolatedAsyncioTestCase):
    """The web pipeline traces a protected, cleaned test array, ``Pipeline._validate`` reads the
    colour order from the new ``trace.json``'s ``colours.included``, and the export proceeds past
    a changed explicit protected component's warning."""

    OMITTED_HEX = "#CF632C"  # Burnt Orange: the last palette colour, so palette order differs

    def setUp(self) -> None:
        SCRATCH.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def job(self, api: _Api, kind: str, project_id: str, revision_id: str, **body) -> str:
        response = await api.client.post("/api/jobs", json={
            "kind": kind, "project_id": project_id, "revision_id": revision_id, **body})
        self.assertEqual(response.status_code, 202, response.text)
        deadline = time.monotonic() + TRACE_BOUND_S
        while time.monotonic() < deadline:
            job = (await api.client.get(f"/api/jobs/{response.json()['id']}")).json()
            if job["status"] not in {"queued", "running"}:
                self.assertEqual(job["status"], "succeeded", job)
                return job["result_revision_id"]
            await asyncio.sleep(0.05)
        self.fail(f"the {kind} job did not finish")

    async def test_validate_reads_the_included_colours_and_export_proceeds_past_the_warning(self):
        subject = build_subject(self.root / "web")
        ExplicitProtectionTest.protect(subject)
        palette = fixture_palette()
        included = [i for i, row in enumerate(palette) if row["hex"] != self.OMITTED_HEX]
        async with _Api(self, self.root / "data") as api:
            project_id, palette_revision = await api.palette_revision(subject)
            cleaned = await self.job(api, "cleanup", project_id, palette_revision,
                                     cleanup={"remove_colour": self.OMITTED_HEX})
            traced = await self.job(api, "trace", project_id, cleaned)
            audit = json.loads(await api.artifact(traced, "trace-audit"))
            self.assertEqual([row["hex"] for row in audit["colours"]["included"]],
                             [palette[i]["hex"] for i in included])
            validated = await self.job(api, "validate", project_id, traced)
            validation = json.loads(await api.artifact(validated, "validation"))
            self.assertTrue(validation["edge"]["passed"], validation)
            self.assertEqual(validation["edge"]["layer_count"], len(included))
            exported = await self.job(api, "export", project_id, validated)
            with zipfile.ZipFile(io.BytesIO(await api.artifact(exported, "export"))) as archive:
                self.assertIn("traced-edge.svg", archive.namelist())
                self.assertIn("map.json", archive.namelist())
            labels = np.load(FIXTURE / "labels.npy")
            named = [w for w in audit["warnings"]
                     if bounds_pattern(component_at(labels, *ExplicitProtectionTest.CHANGED[0]))
                     .search(w)]
            self.assertTrue(named, audit["warnings"])


if __name__ == "__main__":
    unittest.main()
