# Generative tiled-upscale stage (pre-flatten)

Status: **implemented.** This document is the plan of record for the generative
tiled-upscale/regen stage in the vinylgen web pipeline, which drives a
locally-running ComfyUI so the user no longer exports the generated image by
hand, upscales it in a separate ComfyUI tool, and reimports it. It is live in
`vinylgen/web/{comfy_client,upscale}.py`, the `upscale` job kind, the
`POST /api/projects/{id}/revisions/{rev}/upscale` and `GET /api/upscale/options`
routes, and the frontend "Upscale source" action.

## 1. Problem

`CodexImageProvider` returns art at one of three fixed OpenAI-style canvases
(`1536x1024`, `1024x1536`, `1024x1024`, roughly 1.5 MP). Anything larger is
currently faked in `GenerationRunner.run` with `ImageOps.fit(..., NEAREST)` — a
blocky pixel-doubling that adds no detail. Real resolution and detail come from
running the generated image through a tiled generative upscale (an upscale model
followed by a tile-by-tile diffusion redraw under a tile ControlNet), which the
user does manually today. On-disk evidence of the manual round trip:
`outputs/sourceup/source.png` (5980x4208, 4x) and `outputs/griffin-sourceup-2x/`
(2990x2104, 2x).

## 2. What it is, and how it relates to `flatten_upscale`

Two distinct upscales sit on opposite sides of the flatten boundary. They are
**complementary, not alternatives**, and are kept as separate controls at
separate stages — never merged into one selector, because merging would collapse
a deterministic geometry scale and a probabilistic content change into one knob,
against the project rule to keep deterministic guarantees separate from
generative judgments.

|  | `flatten_upscale` (existing) | tiled-regen upscale (this stage) |
|---|---|---|
| Stage | post-flatten, inside the flatten stage | pre-flatten, before palette inference |
| Input | segmented palette-index grid | raw full-colour generated/imported art |
| Method | nearest-neighbour scale + optional Scale2x palette-only smoothing (`vinylgen/palette.py` `_scale2x_labels`) | upscale model + tiled diffusion redraw via ComfyUI `TiledRegen` |
| Nature | deterministic, palette-safe, adds no colours | generative, probabilistic (sampler + seed) |
| Purpose | trace precision on the same artwork | add and clean real art detail |
| Versioning | reusable geometry, no new version | creates a new immutable revision |
| Factor | integer 1-4 (`web/models.py` `flatten_upscale`) | continuous 1-4, step 0.25 |
| UI copy | "Output upscale (x)" | "Source detail upscale (x)" |

The natural full workflow composes both: tiled-regen upscale (add detail to the
1.5 MP generation) -> flatten -> `flatten_upscale` (crisp trace resolution).

## 3. Placement in the pipeline

A new, **manual, optional** job kind and revision stage `upscale`, running on the
existing `JobManager`/SSE/cancellation machinery. It never auto-chains after a
generation; the user triggers it explicitly on a chosen revision.

```
generate (Codex) --> revision "generated-source"
                          |
                    [upscale stage]  (manual, optional; this document)
                          v
                 revision "upscaled-source"  (source.png = tiled-regen output)
                          |
                    palette --> trace --> validate --> export   (unchanged)
```

Because tiled regen runs a sampler, it is generative and must produce a new
immutable, parent-linked revision rather than mutate its parent (project rule:
"any generative edit must create a new version"). This also makes the stage
re-runnable with different settings and applicable to imported sources, not only
generated ones.

## 4. ComfyUI integration

ComfyUI runs as a persistent local systemd service (`comfyui.service`,
`Restart=on-failure`) at `http://127.0.0.1:8188`. vinylgen talks to it
**directly** over HTTP; the separate ComfyUI mobile-ui backend (port 8190) is not
used because its `/api/regen` route is bound to that project's own gallery
database and cannot accept vinylgen images.

Base URL from the `COMFYUI_URL` environment variable, default
`http://127.0.0.1:8188`.

### 4.1 New module `vinylgen/web/comfy_client.py`

A thin HTTP + websocket client, mirroring the reference helpers in the ComfyUI
platform's `scripts/generate.py`:

- `upload_image(png_bytes) -> name` — multipart `POST /upload/image`; the returned
  `name` is wired into the graph's `LoadImage` node.
- `submit_prompt(graph, client_id) -> prompt_id` — `POST /prompt` with
  `{"prompt": graph, "client_id": client_id}`; raise on `error` / `node_errors`.
- `await_history(prompt_id, timeout)` — poll `GET /history/{prompt_id}` (2 s
  interval); raise on `status_str == "error"`; bounded timeout (default 3600 s).
- `output_images(history_entry)` -> the `outputs[node]["images"]` descriptors.
- `download_image(desc) -> bytes` — `GET /view?filename=&subfolder=&type=`.
- `object_info_options(node_class, input_name) -> list[str]` — `GET
  /object_info/{node_class}`, extract the COMBO options (used to enumerate
  upscale models, checkpoints, controlnets).
- `interrupt()` — best-effort `POST /interrupt` for cancellation.
- A websocket reader on `ws://127.0.0.1:8188/ws?clientId=<id>` yielding progress
  and per-tile frames for SSE relay.

### 4.2 New module `vinylgen/web/upscale.py`

Mirrors the `vinylgen/web/generation.py` provider pattern:

- `UpscaleSettings` (see section 5).
- A ported, trimmed graph builder (from the reference `regen_graph.py`
  `build_regen_graph`): `CheckpointLoaderSimple -> LoadImage -> CLIPTextEncode x2
  -> ControlNetApplyAdvanced (tile ControlNet) -> TiledRegen (+ UpscaleModelLoader)
  -> SaveImage`. LoRA and clip-skip wiring are dropped (flat art does not need
  them). The pure tile-geometry helpers (`tile_grid`, `expected_tile_total`,
  `resolved_upscale_by`) and the `validate_sample_size` VRAM guard are ported
  verbatim.
- `TiledRegenProvider.upscale(*, image: bytes, settings, cancelled, progress) ->
  GeneratedImage` — builds the graph, uploads, submits, relays tile progress,
  downloads the redrawn output (`TiledRegen` output 0 = redrawn canvas;
  output 1 = pre-redraw upscaled canvas, unused here).
- `UpscaleRunner` — resolves the parent revision's `source.png`, runs the
  provider, commits a new `upscaled-source` revision via
  `Store.create_upscaled_source(...)` with
  `stale_concerns = ["palette", "trace", "validation", "export"]`.

The fixed prompt reuses the reference's tuned "improve, do not change" positive
and negative text, which is well suited to flat limited-colour art.

### 4.3 `TiledRegen` node

Custom node in the ComfyUI install
(`comfyui/custom_nodes/tiled_regen/__init__.py`), a wrapper over Ultimate SD
Upscale. Confirmed live schema: required inputs `image, model, positive,
negative, vae, upscale_by, seed, steps, cfg, sampler_name, scheduler, denoise,
ring_inner_fraction, ring_feather, mode_type, tile_width, tile_height, mask_blur,
tile_padding, seam_fix_mode, seam_fix_denoise, seam_fix_width, seam_fix_mask_blur,
seam_fix_padding, force_uniform_tiles, tiled_decode, sample_size`; optional
`upscale_model, upscaled_image, only_tile_col, only_tile_row`; outputs
`[IMAGE (redrawn), upscaled (pre-redraw canvas)]`.

## 5. Settings

`UpscaleSettings` (pydantic, in `vinylgen/web/models.py`). User-facing fields and
their vinylgen defaults:

| Field | Default | Range / notes |
|---|---|---|
| `upscaler` | `4xNomos8kSCHAT-S.pth` | a model filename from `/object_info`, or `none` / `lanczos`; validated |
| `upscale_factor` | `2.0` | continuous `1.0-4.0`, step `0.25` (ignored when `upscaler=none`) |
| `checkpoint` | `blue_pencil-XL-v7.0.0.safetensors` | validated against `/object_info` |
| `denoise` | `0.7` | `0.0-1.0` |
| `controlnet` | `true` | tile ControlNet on |
| `controlnet_strength` | `0.4` | `0.0-2.0` |
| `sample_size` | `1024` | `512-1024`; hard cap 1024 (8 GB VRAM) |
| `tile_size` | `512` | grid subdivision |
| `seed` | random | recorded on the revision for recoverability |

The Nomos / denoise 0.7 / ControlNet 0.4 values are the user's tuned operating
point (a heavier redraw and looser structural lock than the ComfyUI form's
"barely touch it" defaults). Everything else — steps, cfg, sampler/scheduler,
mask_blur, tile_padding, the seam-fix block, ring pinning, `tiled_decode` — is
locked to the tuned reference defaults and not surfaced in the UI.

`StartUpscale` request: `{ settings: UpscaleSettings }` against a revision.

## 6. Routes, jobs, store

- `POST /api/projects/{id}/revisions/{rev}/upscale` -> 202 `{project, job}`,
  job kind `upscale`.
- `GET /api/upscale/options` -> `available: true` with the enumerated upscale
  models, checkpoints, controlnets, plus tuned defaults and `sample_size_max`,
  read live from `/object_info`; `200 {"available": false}` when ComfyUI cannot
  be reached; 502 when ComfyUI is reachable but its `/object_info` errors.
- `vinylgen/web/jobs.py`: `start_upscale` / `_run_upscale`, offloading the
  blocking ComfyUI round trip to the existing `ThreadPoolExecutor`, streaming
  tile progress ("Tile k/N", N from `expected_tile_total`) through `_update`/SSE.
  Cancellation maps to `comfy_client.interrupt()` plus stopping the poll; a
  cancelled run never commits.
- `vinylgen/web/store.py`: `create_upscaled_source` following the
  `create_generated_source` blob/revision pattern
  (`blob_relpath(slug, "upscaled-source", now, "source.png")`), persisting the
  full effective settings, checkpoint, and seed for auditability.

## 7. Frontend

A manual, optional trigger on a revision (button/modal) with the small settings
surface from section 5: upscaler select (Nomos default), continuous factor
slider (1-4, step 0.25, labelled "Source detail upscale (x)"), denoise,
ControlNet toggle + strength. Options fetched from `/api/upscale/options`; live
tile progress via the existing SSE channel. The single-tile "test one tile"
preview from the ComfyUI form is out of scope for the first version.

## 8. Failure modes (all surfaced as job errors, none silent)

- ComfyUI unreachable -> the user sees exactly "ComfyUI is not running." and
  nothing else (no URL, no proxy error page). The options route reports
  `available: false` and the upscale dialog shows only that message and its
  Close button; the start route answers 409 with that message as its `detail`
  (a 4xx, because the public reverse proxy replaces 5xx bodies with its own
  HTML page); a job whose ComfyUI calls fail to connect mid-run fails with that
  message as its `error`. Generation and the deterministic pipeline are
  unaffected. The frontend's `result()` shows a JSON `detail` and otherwise only
  "Server error <status> (<statusText>)", never a raw response body.
- Missing checkpoint / upscale / controlnet file -> validated against
  `/object_info` up front, rejected before queueing.
- VRAM OOM or sampler error -> ComfyUI reports `status_str == "error"`; propagate
  its message.
- Timeout -> bounded poll, configurable.

## 9. Testing

- Graph-builder + tile-geometry parity against a captured reference graph; the
  `validate_sample_size` guard rejects `> 1024`.
- `comfy_client` against a mock HTTP server (upload -> prompt -> history -> view
  happy path; error status; timeout). No live ComfyUI in the suite.
- `UpscaleRunner` with a fake provider: commits an `upscaled-source` revision,
  sets `stale_concerns`, honours cancellation at the commit boundary.
- One optional, skipped-by-default live smoke test against `:8188`.
- Adversarial review by a non-implementer before the task is marked done.

## 10. Definition of done (docs to update at implementation time)

`docs/architecture.md` (add the `upscale` stage to the sequence and the
generation-branch section), `docs/files-and-outputs.md` (the `upscaled-source`
revision and its `source.png` artifact), and this file's status line. Those
current-truth docs are updated only when the stage actually ships, not now.

## 11. Non-goals

Single-tile test/preview UI; LoRA / clip-skip; the whole-image and masked
generative-edit tree (separate pending worklog tasks — this is resolution
upscale, a different concern); auto-installing ComfyUI or its models; the
standalone CLI path (`palette_infer.py`). Web app only.
