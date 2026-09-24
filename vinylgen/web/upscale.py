"""Tiled generative upscale stage, driving a local ComfyUI ``TiledRegen`` node.

Mirrors the ``vinylgen/web/generation.py`` provider pattern for a different
model round trip: instead of asking Codex for a new image, it runs an existing
source image through an upscale model followed by a tile-by-tile diffusion
redraw under a tile ControlNet, adding real resolution and detail before the
deterministic palette pipeline sees it.

The graph builder is pure — it performs no I/O and reads no image metadata, so
an imported source builds the same graph as a generated one. It ports the tuned
"improve, do not change" operating point and the tile-geometry helpers from the
ComfyUI platform's ``regen_graph.py``, trimmed to the single shape this stage
runs: the tiled full run. ``TiledRegenProvider`` uploads the source, submits the
graph, polls to completion and returns the redrawn canvas; ``UpscaleRunner``
resolves the parent revision's source, runs the provider and commits the result
as a new immutable, parent-linked ``upscaled-source`` revision.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import threading
import uuid
from io import BytesIO
from typing import Any, Callable

from PIL import Image

from .comfy_client import ComfyClient
from .generation import GeneratedImage
from .models import UpscaleSettings
from .store import Store

# ---------------------------------------------------------------------------
# Defaults — the tuned "improve, don't change" operating point
# ---------------------------------------------------------------------------

DEFAULT_POSITIVE = (
    "high-definition coherent illustration, preserve the exact composition, "
    "perspective, subjects, object count, lighting and palette; reconstruct "
    "ambiguous details as clean intentional shapes, confident continuous "
    "contours, controlled line weight, crisp edges, coherent geometry and anatomy"
)
DEFAULT_NEGATIVE = (
    "altered composition, new objects, missing objects, duplicate subjects, "
    "malformed anatomy, broken geometry, blurry outlines, fuzzy details, random "
    "lines, noisy patches, text artifacts, color shift, global brightening, "
    "washed shadows, painterly smearing, bloom"
)

DEFAULT_CHECKPOINT = "blue_pencil-XL-v7.0.0.safetensors"

DEFAULT_STEPS = 30
DEFAULT_CFG = 5.0
DEFAULT_SAMPLER = "dpmpp_2m_sde"
DEFAULT_SCHEDULER = "karras"
DEFAULT_DENOISE = 0.7

DEFAULT_CONTROLNET_MODEL = "xinsir-controlnet-tile-sdxl-1.0.safetensors"
DEFAULT_CONTROLNET_STRENGTH = 0.4
DEFAULT_CONTROLNET_START = 0.0
DEFAULT_CONTROLNET_END = 1.0

DEFAULT_UPSCALER = "4xNomos8kSCHAT-S.pth"
DEFAULT_UPSCALE_FACTOR = 2.0
DEFAULT_TILE_WIDTH = 512
DEFAULT_TILE_HEIGHT = 512
DEFAULT_MASK_BLUR = 16
DEFAULT_TILE_PADDING = 96
DEFAULT_MODE_TYPE = "Chess"
DEFAULT_SEAM_FIX_MODE = "None"
DEFAULT_SEAM_FIX_DENOISE = 0.25
DEFAULT_SEAM_FIX_WIDTH = 96
DEFAULT_SEAM_FIX_MASK_BLUR = 16
DEFAULT_SEAM_FIX_PADDING = 32
# The 8GB card decodes a 512 tile comfortably but a full 4x canvas does not;
# tiled decode keeps the VAE pass inside VRAM at every canvas size.
DEFAULT_TILED_DECODE = True
# 0.0 pins the padding: the redraw mask is exactly the tile grown by mask_blur,
# so the padded context is composited back unchanged.
DEFAULT_RING_INNER_FRACTION = 0.0
DEFAULT_RING_FEATHER = 16
DEFAULT_SAMPLE_SIZE = 1024

UPSCALER_NONE = "none"
UPSCALER_LANCZOS = "lanczos"

# Progress milestones the provider reports. The tiled run itself streams
# "Tile k/N" between _TILE_PROGRESS_START and _TILE_PROGRESS_END; the fixed
# points bracket it so the bar is monotonic across upload, run and download.
_PROGRESS_UPLOAD = 0.1
_PROGRESS_RUN = 0.15
_TILE_PROGRESS_START = 0.2
_TILE_PROGRESS_END = 0.9
_PROGRESS_DOWNLOAD = 0.95

# ---------------------------------------------------------------------------
# VRAM guard — SDXL already sits at ~87% of the 8GB card at 1024²
# ---------------------------------------------------------------------------

# The sampling resolution is what costs VRAM. ``sample_size`` is the resolution
# the node samples each padded tile crop at, so the tile size is a grid setting
# and only this value has to stay inside the card.
SAMPLE_SIZE_MAX = 1024


def validate_sample_size(sample_size: int) -> None:
    """Reject a sampling resolution the card cannot hold.

    Raises ``ValueError`` above ``SAMPLE_SIZE_MAX``.
    """
    if sample_size > SAMPLE_SIZE_MAX:
        raise ValueError(
            f"Sample size {sample_size} exceeds the {SAMPLE_SIZE_MAX}px limit — "
            "the 8GB card runs out of VRAM sampling at that resolution."
        )


# ---------------------------------------------------------------------------
# Tile geometry — mirrors TiledRegen.execute
# ---------------------------------------------------------------------------


def tile_grid(
    source_width: int,
    source_height: int,
    upscale_factor: float,
    tile_width: int,
    tile_height: int,
) -> tuple[int, int, int, int]:
    """``(cols, rows, canvas_width, canvas_height)`` for one tiled run.

    The canvas is rounded to a multiple of 8 exactly as USDU's ``round_length``
    does, and the grid is the ceiling of the canvas over the tile size. Every
    other geometry helper here is built on this one, so the tile count and the
    node's own loops read the same grid.
    """
    canvas_width = round(source_width * upscale_factor / 8) * 8
    canvas_height = round(source_height * upscale_factor / 8) * 8
    cols = math.ceil(canvas_width / tile_width)
    rows = math.ceil(canvas_height / tile_height)
    return cols, rows, canvas_width, canvas_height


def expected_tile_total(
    source_width: int,
    source_height: int,
    upscale_by: float,
    tile_width: int,
    tile_height: int,
    mode_type: str,
    seam_fix_mode: str,
) -> int:
    """Tile count the ``TiledRegen`` node will report for this geometry.

    The grid comes from ``tile_grid`` and each seam-fix mode adds its own band
    count on top of the redraw pass.
    """
    cols, rows, _canvas_width, _canvas_height = tile_grid(
        source_width, source_height, upscale_by, tile_width, tile_height
    )

    redraw_tiles = 0 if mode_type == "None" else rows * cols

    if seam_fix_mode == "Half Tile":
        seam_tiles = (rows - 1) * cols + (cols - 1) * rows
    elif seam_fix_mode == "Half Tile + Intersections":
        seam_tiles = (rows - 1) * cols + (cols - 1) * rows + (rows - 1) * (cols - 1)
    else:
        seam_tiles = 0

    return max(1, redraw_tiles + seam_tiles)


def resolved_upscale_by(settings: UpscaleSettings) -> float:
    """The factor ``TiledRegen`` scales the canvas by. "none" regenerates 1:1."""
    return (
        1.0 if settings.upscaler == UPSCALER_NONE else float(settings.upscale_factor)
    )


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


class _Graph:
    """Accumulates nodes under sequential numeric ids."""

    def __init__(self) -> None:
        self.nodes: dict = {}
        self._next = 1

    def add(self, class_type: str, inputs: dict) -> str:
        node_id = str(self._next)
        self._next += 1
        self.nodes[node_id] = {"class_type": class_type, "inputs": inputs}
        return node_id


def build_upscale_graph(
    settings: UpscaleSettings, *, image_filename: str, seed: int
) -> dict:
    """Build the ComfyUI graph for one tiled generative upscale.

    ``image_filename`` is the name the source image was uploaded under and
    ``seed`` the resolved sampler seed. The chain is CheckpointLoaderSimple ->
    LoadImage -> CLIPTextEncode x2 (the tuned improve-don't-change text) ->
    ControlNetApplyAdvanced (tile ControlNet, while ``controlnet`` is on) ->
    TiledRegen -> SaveImage, with an ``UpscaleModelLoader`` wired to the node's
    ``upscale_model`` input only when a model upscaler is chosen. ``TiledRegen``
    output 0 is the redrawn canvas the SaveImage persists.
    """
    graph = _Graph()

    ckpt = graph.add("CheckpointLoaderSimple", {"ckpt_name": settings.checkpoint})
    load = graph.add("LoadImage", {"image": image_filename})

    pos = graph.add("CLIPTextEncode", {"text": DEFAULT_POSITIVE, "clip": [ckpt, 1]})
    neg = graph.add("CLIPTextEncode", {"text": DEFAULT_NEGATIVE, "clip": [ckpt, 1]})

    positive_ref = [pos, 0]
    negative_ref = [neg, 0]
    if settings.controlnet:
        cn_loader = graph.add(
            "ControlNetLoader", {"control_net_name": DEFAULT_CONTROLNET_MODEL}
        )
        cn_apply = graph.add(
            "ControlNetApplyAdvanced",
            {
                "positive": [pos, 0],
                "negative": [neg, 0],
                "control_net": [cn_loader, 0],
                # The unmodified source image is the tile hint; USDU crops it per
                # tile alongside the rest of the conditioning.
                "image": [load, 0],
                "strength": settings.controlnet_strength,
                "start_percent": DEFAULT_CONTROLNET_START,
                "end_percent": DEFAULT_CONTROLNET_END,
            },
        )
        positive_ref = [cn_apply, 0]
        negative_ref = [cn_apply, 1]

    upscale_model_ref = None
    if settings.upscaler not in (UPSCALER_NONE, UPSCALER_LANCZOS):
        upscaler = graph.add("UpscaleModelLoader", {"model_name": settings.upscaler})
        upscale_model_ref = [upscaler, 0]

    tiled_inputs = {
        "image": [load, 0],
        "model": [ckpt, 0],
        "positive": positive_ref,
        "negative": negative_ref,
        "vae": [ckpt, 2],
        "upscale_by": resolved_upscale_by(settings),
        "seed": seed,
        "steps": DEFAULT_STEPS,
        "cfg": DEFAULT_CFG,
        "sampler_name": DEFAULT_SAMPLER,
        "scheduler": DEFAULT_SCHEDULER,
        "denoise": settings.denoise,
        "ring_inner_fraction": DEFAULT_RING_INNER_FRACTION,
        "ring_feather": DEFAULT_RING_FEATHER,
        "mode_type": DEFAULT_MODE_TYPE,
        "tile_width": settings.tile_size,
        "tile_height": settings.tile_size,
        "sample_size": settings.sample_size,
        "mask_blur": DEFAULT_MASK_BLUR,
        "tile_padding": DEFAULT_TILE_PADDING,
        "seam_fix_mode": DEFAULT_SEAM_FIX_MODE,
        "seam_fix_denoise": DEFAULT_SEAM_FIX_DENOISE,
        "seam_fix_width": DEFAULT_SEAM_FIX_WIDTH,
        "seam_fix_mask_blur": DEFAULT_SEAM_FIX_MASK_BLUR,
        "seam_fix_padding": DEFAULT_SEAM_FIX_PADDING,
        # Every crop is resized to sample_size, which the node only honours with
        # uniform tiles; the non-uniform sizing is not offered.
        "force_uniform_tiles": True,
        "tiled_decode": DEFAULT_TILED_DECODE,
        "only_tile_col": -1,
        "only_tile_row": -1,
    }
    if upscale_model_ref is not None:
        tiled_inputs["upscale_model"] = upscale_model_ref
    result = graph.add("TiledRegen", tiled_inputs)

    graph.add(
        "SaveImage",
        {"images": [result, 0], "filename_prefix": "tiled_regen"},
    )
    return graph.nodes


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


def _random_seed() -> int:
    """A fresh sampler seed in the ComfyUI-accepted ``0 .. 2**32 - 1`` range."""
    return random.randrange(0, 2 ** 32)


def _random_client_id() -> str:
    """A per-submission websocket client id, matching ComfyUI's uuid form."""
    return uuid.uuid4().hex


def _source_dimensions(image: bytes) -> tuple[int, int]:
    """The ``(width, height)`` of an encoded source image in pixels."""
    with Image.open(BytesIO(image)) as handle:
        return handle.width, handle.height


def _decode_tile_frame(raw: bytes) -> dict | None:
    """The metadata of a ``TiledRegen`` tile preview binary frame, or ``None``.

    Mirrors the reference frontend's ``parsePreviewFrame`` rule: a frame is a
    tile iff it is preview event ``4`` (``PREVIEW_IMAGE_WITH_METADATA``) and its
    JSON metadata carries ``kind == "tile"``. Only the metadata is read here —
    it carries the tile index and per-step counters — and the JPEG payload that
    follows it is ignored. Every other preview frame is the running composite.
    """
    if len(raw) < 8:
        return None
    if int.from_bytes(raw[0:4], "big") != 4:
        return None
    meta_len = int.from_bytes(raw[4:8], "big")
    if meta_len > len(raw) - 8:
        return None
    try:
        metadata = json.loads(raw[8:8 + meta_len].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(metadata, dict) or metadata.get("kind") != "tile":
        return None
    return metadata


class _TileProgressRelay:
    """Translate ComfyUI websocket frames into live "Tile k/N" progress.

    Runs the client's ``iter_progress`` websocket reader on a daemon thread for
    the lifetime of one tiled-regen run. The authoritative per-tile counter is
    the stock ComfyUI ``progress`` text frame — ``{"type": "progress", "data":
    {"value": k, "max": N}}`` — which ``TiledRegen`` drives once per finished
    tile from its single job-level progress bar. The binary tile-preview frames
    (event ``4`` with ``kind == "tile"``) refine the fraction with per-step
    sub-progress inside the tile currently sampling. ``total_tiles`` is computed
    up front from the source geometry so the counter is bounded even if a frame
    type is unavailable, and the reported fraction only ever advances.
    """

    def __init__(self, client: ComfyClient, client_id: str, total_tiles: int,
                 progress: Callable[[float, str], None],
                 cancelled: Callable[[], bool]) -> None:
        self.client = client
        self.client_id = client_id
        self.total_tiles = max(1, total_tiles)
        self.progress = progress
        self.cancelled = cancelled
        self._stop = threading.Event()
        self._last_fraction = _TILE_PROGRESS_START
        self._thread = threading.Thread(
            target=self._run, name="vinylgen-tile-progress", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        frames = self.client.iter_progress(
            self.client_id, should_stop=self._stop.is_set)
        try:
            for frame in frames:
                if self._stop.is_set() or self.cancelled():
                    break
                self._handle(frame)
        except BaseException:
            # A best-effort progress relay must never surface its own failure;
            # the HTTP poll owns completion. A cancellation raised through the
            # progress callback is asyncio.CancelledError (a BaseException), so
            # it is caught here too and simply ends the reader.
            pass
        finally:
            frames.close()

    def _handle(self, frame: dict | bytes) -> None:
        if isinstance(frame, dict):
            if frame.get("type") != "progress":
                return
            data = frame.get("data") or {}
            value = data.get("value")
            if not isinstance(value, int):
                return
            maximum = data.get("max")
            total = maximum if isinstance(maximum, int) and maximum > 0 else self.total_tiles
            # A stock progress frame fires once tile ``value`` has finished.
            self._report(value, total, value)
            return
        metadata = _decode_tile_frame(bytes(frame))
        if metadata is None:
            return
        index = metadata.get("tile_index")
        if not isinstance(index, int):
            return
        total = metadata.get("tile_total")
        total = total if isinstance(total, int) and total > 0 else self.total_tiles
        step = metadata.get("step")
        steps = metadata.get("steps")
        if isinstance(step, int) and isinstance(steps, int) and steps > 0:
            completed = (index - 1) + min(step, steps) / steps
        else:
            completed = index - 1
        self._report(index, total, completed)

    def _report(self, tile: int, total: int, completed: float) -> None:
        total = max(1, total)
        span = _TILE_PROGRESS_END - _TILE_PROGRESS_START
        fraction = _TILE_PROGRESS_START + span * max(0.0, min(1.0, completed / total))
        fraction = max(fraction, self._last_fraction)
        self._last_fraction = fraction
        self.progress(fraction, f"Tile {min(max(tile, 1), total)}/{total}")


class UpscaleProvider:
    name: str

    def upscale(self, *, image: bytes, settings: UpscaleSettings,
                cancelled: Callable[[], bool] = lambda: False,
                progress: Callable[[float, str], None] | None = None) -> GeneratedImage: ...


class TiledRegenProvider:
    """Run one image through ComfyUI's tiled generative upscale and redraw."""

    name = "tiled-regen"

    def __init__(self, client: ComfyClient | None = None) -> None:
        self.client = client or ComfyClient()

    def upscale(self, *, image: bytes, settings: UpscaleSettings,
                cancelled: Callable[[], bool] = lambda: False,
                progress: Callable[[float, str], None] | None = None) -> GeneratedImage:
        validate_sample_size(settings.sample_size)
        seed = settings.seed if settings.seed is not None else _random_seed()
        if cancelled():
            raise asyncio.CancelledError
        if progress:
            progress(_PROGRESS_UPLOAD, "Uploading source to ComfyUI")
        image_name = self.client.upload_image(image, filename="source.png")

        # The tile total the node will report, computed from the source pixels
        # with the same locked mode_type/seam_fix the graph builder wires in, so
        # the relay's N matches the node's own grid.
        source_width, source_height = _source_dimensions(image)
        total_tiles = expected_tile_total(
            source_width, source_height, resolved_upscale_by(settings),
            settings.tile_size, settings.tile_size,
            DEFAULT_MODE_TYPE, DEFAULT_SEAM_FIX_MODE)

        graph = build_upscale_graph(settings, image_filename=image_name, seed=seed)
        client_id = _random_client_id()
        relay = None
        if progress:
            relay = _TileProgressRelay(
                self.client, client_id, total_tiles, progress, cancelled)
            # Connect the websocket reader before submitting so no early tile
            # frame is missed between the queue and the first read.
            relay.start()
        try:
            prompt_id = self.client.submit_prompt(graph, client_id)
            if progress:
                progress(_PROGRESS_RUN,
                         f"Running tiled upscale in ComfyUI ({total_tiles} tiles)")
            entry = self.client.await_history(prompt_id, cancelled=cancelled)
        finally:
            if relay is not None:
                relay.stop()
        if cancelled():
            raise asyncio.CancelledError
        images = self.client.output_images(entry)
        if not images:
            raise RuntimeError("ComfyUI tiled upscale produced no output image")
        # TiledRegen output 0 (the redrawn canvas) is the only saved image.
        content = self.client.download_image(images[0])
        if progress:
            progress(_PROGRESS_DOWNLOAD, "Downloaded upscaled source")
        metadata: dict[str, Any] = {
            **settings.model_dump(), "seed": seed, "provider": self.name,
        }
        return GeneratedImage(content, "image/png", metadata)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class UpscaleRunner:
    def __init__(self, store: Store, provider: UpscaleProvider) -> None:
        self.store = store
        self.provider = provider

    def run(self, parent_revision_id: str, settings: UpscaleSettings,
            progress: Callable[[float, str], None],
            cancelled: Callable[[], bool] = lambda: False,
            commit: Callable[[Callable[[], str]], str] = lambda operation: operation()) -> str:
        source = self._read_source(parent_revision_id)
        seed = settings.seed if settings.seed is not None else _random_seed()
        resolved = settings.model_copy(update={"seed": seed})
        progress(.1, "Preparing tiled upscale")
        result = self.provider.upscale(image=source, settings=resolved,
                                       cancelled=cancelled, progress=progress)
        if cancelled():
            raise asyncio.CancelledError
        progress(.9, "Persisting immutable upscaled source")
        # Both pixel grids are recorded so settings picked in source-pixel coordinates
        # on either side of this revision (the Flatten form's preservation points) can
        # be mapped onto the other grid; the output size is the decoded image's own,
        # never the nominal factor, since the node rounds it to a multiple of 8.
        grids = {"parent_source_size": list(_source_dimensions(source)),
                 "source_size": list(_source_dimensions(result.content))}
        revision_id = commit(lambda: self.store.create_upscaled_source(
            parent_revision_id, message="Tiled generative upscale",
            settings={**resolved.model_dump(), **grids}, checkpoint=resolved.checkpoint,
            seed=seed, source=BytesIO(result.content)))
        progress(.98, "Upscaled source revision committed")
        return revision_id

    def _read_source(self, revision_id: str) -> bytes:
        """The source.png bytes visible from ``revision_id`` through its ancestry."""
        artifacts = self.store.effective_artifacts(revision_id)
        source = artifacts.get("source")
        if source is None:
            raise ValueError("revision is missing its source artifact")
        return self.store.artifact_path(source["id"]).read_bytes()
