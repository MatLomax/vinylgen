# Pipeline Architecture

The production pipeline is a sequence of explicit concerns. No stage silently
reconstructs data owned by an earlier stage.

```text
source raster
  -> colour.py         colour conversion, clustering primitives, confidence
  -> palette.py        palette inference orchestration
  -> segmentation.py   AA rejection and intentional thin-feature protection
  -> contracts.py      versioned segmentation.npz handoff
  -> cutter_cleanup.py cleanup stage: physical size, removed colour, optional
                       physical feature removal/widening of the labels
  -> tracing.py        trace stage: one t3api.trace call on the cleaned labels,
                       map.json (the planar-map file), its gate, the audit
  -> svg_writer.py     the edge SVG from map.json (add_face_layer), serialization
  -> validation.py     SVG structural validation, the semantic audit
     cleanup_window.py cutter cleanup of one window, for the live cleanup preview
```

`reporting.py` owns palette inspection images. `cli_palette.py` and
`cli_trace.py` own argument/config parsing. The top-level `palette_infer.py` and
`svg_trace.py` files are executable entry points only.

The web application calls the same Python orchestration functions through
typed background jobs in `vinylgen/web/pipeline.py`; it never constructs shell
commands for palette inference, cleanup or tracing. Successful stages create
immutable, parent-linked revisions. Their settings record the complete
effective configuration of their stage plus concern-level downstream staleness.
Artifact lookup walks revision ancestry, so a trace revision reuses its
parent's source and segmentation without copying blobs. A trace revision
supersedes everything made of an older run (its trace outputs, Inkscape edit,
validation and export), so export requires a validation of the current run.
Likewise a flatten (`palette`) revision supersedes every flatten output of an
older run, including the optional `protected-areas-overlay.png` a run that
protected nothing does not write, and the overlay belongs to the palette
concern a source upscale marks stale. Revisions derived from the flatten
(palette and pencil edits, cleanup, trace) inherit the overlay with the rest of
the flatten, since they carry its explicit protection unchanged.

The web workflow is Flatten, Palette, Cleanup, Edit, Trace, Nodes (nib's editor on
the trace's planar map, below). Cleanup
(`vinylgen/web/cleanup.py`) owns the physical output size, the removed colour
and the cutter cleanup. A `cleanup` revision records them under
`settings.cleanup`. A cleanup always starts from the raster before any
earlier cleanup, so re-running it with cleanup off, another size or other
thresholds replaces that cleanup rather than stacking on it. The user's
explicit protection wins: explicitly protected detail is never removed or
consumed. When the run changed pixels the revision also holds the cleaned
`flattened.png`, a `segmentation.npz` whose labels are cleaned and whose
automatic protection is cleared where labels changed (palette costs, explicit
protection and palette carried unchanged), refreshed palette shares and a
re-rendered `report.png`. A run that changed nothing records only the size,
removed colour and its raster's palette shares, and its effective raster is
the uncleaned one. A run then replays the touch-ups made on the previous
cleanup's raster (`vinylgen/web/touch_ups.py`, see the editors below) on its
cleaned labels, and records the ones it replayed as touch-ups of its own raster,
so the run after it replays them again. The trace job never
cleans: it traces the segmentation it inherits and reads the size and removed
colour from the nearest `cleanup` ancestor, the removed colour by the palette
index that cleanup resolved it to (`pipeline.cleanup_removed_index`), since the
trace's colour names may rename it. When a revision in between marks
cleanup stale (a flatten, a merge, an area or pencil edit made while no cleanup
was current, or a source upscale), the trace is
blocked until Cleanup is run again (the Trace panel's one-click re-run has the
server rebuild that cleanup's settings, naming its removed colour by hex:
`pipeline.rerun_cleanup_settings`); with no cleanup ancestor at all it traces
in pixels and cuts every colour. Trace revisions recorded before the cleanup
stage embed those values in `settings.trace`; they are left as recorded, the
web form shows them in Cleanup, and a new trace from one has no cleanup
ancestor. The trace CLI keeps its size, removed-colour and cleanup flags and
applies the same `cutter_cleanup.apply_cleanup` in memory before tracing,
leaving the subject's files unchanged. Like the web trace, it infers layer
order and validates against the cleaned labels and the raster derived from
them, which is what was traced.

The palette and region editors are deterministic branches between palette
inference and tracing. They use the shared artwork canvas and intentionally
offer no fill, lasso, protection-painting, or manual RGB tools. Whole-colour
merges, name metadata, point-selected four-way connected region reassignment,
and a per-pixel pencil that repaints individual pixels to an existing palette
colour create new parent-linked revisions; `segmentation.labels` remains
authoritative and `flattened.png` is derived mechanically. While a revision's
cleanup is current the editors act on its cleaned raster, which is what Trace
traces; otherwise on the raster from before any cleanup
(`editor.edits_cleaned_raster`). On the cleaned raster an area recolour or a
pencil stroke is a touch-up: the cleanup stays current, the revision records
`settings.cleaned_raster` (its raster artifacts are a cleaned raster, skipped by
the `before_cleanup` lookup like a cleanup's) and the touch-up itself
(`settings.touch_ups` and a `touch-ups.npz` artifact holding the stroke's pixels
or the exact region the recolour changed). Painting with the colour the cleanup
removed erases, since Trace omits that palette index. A rename or recolour
changes no structure, so it keeps the cleanup current and applies to both
rasters: the cleaned raster's artifacts and `before-cleanup-*` artifacts for the
raster a Cleanup run starts from. A merge changes structure: it applies to the
raster from before cleanup and marks the cleanup stale. The next Cleanup run
replays the touch-ups made since the last cleanup, oldest first, in one
revision: a pencil stroke always, an area recolour only when the region under its
click point in the new result is exactly the recorded region in the same colour.
Newer edits win: an area or pencil edit made while the cleanup is stale applies to
the raster from before cleanup and is recorded too, as a footprint (`replays`
false), so an older touch-up replays only on pixels no later footprint covers, and
an area touch-up any later footprint overlaps is skipped. A touch-up painted in
the colour the cleanup removes is recorded as an erase (`erase`): a run paints it
in the colour that run removes (by its removed palette index), and skips it, saying
so, when the run removes none; the live preview does the same. Any other touch-up
paints its own colour, so one whose colour a later run removes is simply erased.
Colours are followed
through later merges by identity and across a re-flatten by exact hex, so a
touch-up whose colour was merged away or is not in the new palette is dropped, as
is every touch-up after a re-flatten at a different size and one a later edit
covers. An erase while the run removes no colour, and an area recolour whose area
has changed since it was edited, are kept, recorded on the run as waiting, and
replayed by a later run once they apply again. The run's result line and
`settings.touch_up_replay` say how many replayed and where each waiting, dropped
or partly superseded one is. While the cleanup is stale, the Edit and Cleanup panels
say how many touch-ups the next run will replay, since the raster before cleanup
does not show them. Every edit that changes the raster or its colours rewrites
`report.png` too.
Edits recorded before touch-ups existed applied to the raster from before
cleanup and are part of it, so they are cleaned with it rather than replayed.
Because connectivity is recomputed from the label grid, a pencil stroke that
severs a region splits it into separate components with no extra bookkeeping.
Undo and redo navigate revision ancestry.

The Flatten stage's preservation tools (protect a connected detail, preserve a
colour family, keep two colours separate) pick source pixels on the same
canvas. Every chosen point is drawn there as a screen-space marker of constant
size that follows pan and zoom: a diamond for a protected detail, a ring for a
preserved colour and two squares joined by a line for a separate pair, each
numbered as its row in the side-panel list, with a pair's first pick shown as a
pending marker. Hovering or focusing a row highlights its marker. The markers
are source-grid positions, scaled onto the Flattened view when
`flatten_upscale` makes that raster larger, and take no pointer input. They are
hidden from assistive technology because the side-panel lists are the
accessible form. While a pick is active the hovered pixel is outlined by a
crosshair with an x, y readout. Each editor canvas offers toggles only for its
own stage's overlays: Flatten offers "Protected areas", Trace offers its two
audit overlays, and the other canvases offer none. An enabled overlay is drawn
over any shown image whose pixel grid it registers with, meaning the same grid
or one integer upscale of it, so a flatten's overlay lines up on both the
Source and Flattened views.

Source generation is a provider-backed branch before deterministic processing.
`vinylgen.web.generation` expands the short subject through a persisted,
deterministic art-direction template and sends only that text plus provider
settings to the selected provider. It never resolves artifacts or supplies an
earlier revision as an implicit reference. A successful request creates a new
root `generated-source` revision containing the short and expanded prompts for
auditability (the expanded provider prompt is not shown in the Create panel),
complete effective settings, provider request metadata, an explicit empty
reference list, and one full-frame `source.png` normalized to the requested
dimensions. Provider output remains probabilistic; only persistence and output
dimensions are deterministic.

Tiled generative upscale is a second provider-backed branch between source
generation and deterministic processing, owned by `vinylgen.web.upscale`. It is
manual and optional: on a source revision the user runs it explicitly, and it
drives the local ComfyUI directly over HTTP (`vinylgen.web.comfy_client`,
`COMFYUI_URL`) to run an upscale model followed by a tile-by-tile diffusion
redraw under a tile ControlNet, adding real detail the low-resolution generation
lacks. Because it runs a sampler it is generative, so it commits a new immutable,
parent-linked `upscaled-source` revision (never mutating its parent) whose
`source.png` is the redrawn canvas and which marks palette, cleanup, trace,
validation, and export stale. It is distinct from the palette stage's `flatten_upscale`,
which is post-flatten, deterministic, and palette-preserving for trace precision;
the two compose. Sampling resolution is capped for the deployment's VRAM, invalid
model selections are rejected before the job is queued, and an unreachable
ComfyUI fails only this branch, leaving generation and the deterministic pipeline
untouched.

The personal deployment's provider is `CodexImageProvider`. It launches the
installed Codex client in a new empty `.tmp/` job directory with `exec`, JSONL
events, and `--ephemeral` for every request. Codex alone owns its ChatGPT login;
Vinylgen neither locates nor parses OAuth material and strips
`OPENAI_API_KEY` from the child environment. The adapter retains only bounded
counts for a fixed allowlist of event types, validates exactly one
decodable PNG, and removes scratch state before the runner crosses the immutable
revision commit boundary. Cancellation and timeout terminate the owned process
group. The direct OpenAI Image API remains an unimplemented contingency, not a
runtime fallback.

The Inkscape handoff is an immutable branch after tracing. SVG and PNG
downloads are explicit choices, and configured shared-folder saves are atomic.
Reimport accepts only passive, self-contained SVG, renders it headlessly with
the shared preview policy (`vinylgen/preview.py`), and stores a neutral edited-SVG artifact, fresh inspection preview,
and validation audit on a new `inkscape-edit` revision. Because an arbitrary
edit cannot honestly be classified as the trace's geometry (that is `map.json`,
from which the edge SVG is exported), trace, validation, and export are stale until retracing, and a retrace hides the
edit's artifacts with the rest of the older run's output. The generated trace on
the parent is untouched.
The hosted service never claims that a phone or remote browser can launch a
desktop Inkscape process.

The Trace canvas draws a running trace live. t3 emits one `face` event per drawn
face as it finishes it (`face`, `label`, `rings` as path data, `nodes` as
`sodipodi:nodetypes`); the trace job's worker (`web/pipeline_worker.py`) relays
each as a JSON line `{"type": "face", ...}`, and the job's SSE stream
(`GET /api/jobs/{id}/events`) carries it as an `event: face` message, before the
job's final status. Events stream in t3's emission order and are never held back
or sorted. The canvas keeps every face received and redraws them on each arrival
in the finished edge SVG's paint order (`web/src/LiveTrace.tsx`): one layer per
palette colour in palette order, the omitted colour drawing none, each layer's
faces in face order. The finished SVG orders a layer's faces by area instead,
but the faces of one layer share one fill and never overlap, so that order
changes no pixel and the composite equals the finished edge SVG. The faces are
dropped when the job ends; the new revision's edge SVG then shows the result. A
client that subscribes late sees only the faces emitted after it subscribed.
There is no windowed trace preview: no trace setting changes t3's fit, and a
window trace would not equal the full trace near its edges (t3's chains run past
any window).

A trace whose parent trace's `map.json` was traced from the same labels (the
same sha256 of the effective `segmentation.npz` labels) by the same
`t3api.BUILD_ID` copies that file, rewriting it only when the new palette names
its colours differently, and calls `t3api.trace` not at all
(`PipelineRunner.reusable_map`): only the edge SVG, the checks and the audit are
rebuilt, with the revision's own palette, omitted colour and limits, and recorded
in its own `trace.json` (`t3.reused_from` names the revision whose map it reused).
A map edited in nib is never reused this way: a trace from an edited revision
traces afresh.

The Nodes stage's canvas area is nib's editor (`web/src/NibEditor.tsx`). It
mounts `@nib/core`'s `Editor` through `mountEditor` on its own canvas, with a
server host: `open` is a GET of the revision's `map.json` artifact, and `save`
is `POST /api/revisions/{revision_id}/map` with the exact text nib wrote. Over
the canvas, in the component's positioned container, it mounts nib's chrome with
`mountChrome(container, editor, { actions: ['save'] })`, styled by
`@nib/core/chrome.css`; the chrome's Save calls the host's `save`, and every
outcome is a line on the chrome's notice. The package is the vendored tarball
`web/vendor/nib-core-0.10.0.tgz`, installed by `file:` path. The route (`web/app.py`) hands the body to
`PipelineRunner.save_map` (`web/pipeline.py`). That function refuses a document
with `nib.map_refusal` (HTTP 422: unreadable, failing `planar_map_check`,
breaking the rule of the map, of another size than the parent's, a palette that
is not the parent's with new colours only appended, a face labelled with no palette
entry, or unchanged). Otherwise it makes a
child trace revision, "edited in nib", through `PipelineRunner._trace`, the one
function that also traces and re-exports. The posted map is written into the
work folder and `trace_subject` rebuilds everything after the trace from it, as
it does for a re-export; a colour nib appended to the palette is drawn as its
own layer after the included ones (`tracing.trace_palette`), and validation
reads the colours from the map the same way. The save runs in the request (in the compute worker
subprocess, as a trace does) and answers 201 with the new revision. The parent's
artifacts are untouched.

The Cleanup canvas has a live preview too (`vinylgen/cleanup_window.py`, served
on a dedicated preview thread at `POST /api/revisions/{id}/cleanup-preview`). Its
"Live preview" switch is remembered in the browser across stage changes and
reloads; with it on, the canvas cleans whatever is zoomed into with the Cleanup form's current,
unsaved values, physical size included (the thresholds are in mm), and draws the
cleaned pixels over the image at that window. On top, on the same raster grid,
it draws what a Cleanup run with those values would change on the raster the
canvas shows (the active cleanup's result, if any): red would be removed, green
would be widened, blue would return to its uncleaned colour; with the settings
of the active cleanup that produced the shown raster it returns that raster and
draws nothing. Separately, the
canvas's "Show changes" switch (remembered) draws what the shown revision's
Cleanup run has changed, over the whole image and with or without the preview,
from the `cleanup-changes-overlay` artifact each Cleanup run that changed pixels
records; it is disabled, with "Re-run Cleanup to record its changes", for a
cleanup recorded before runs wrote it. A legend in the canvas controls names the
colours of each overlay on view. The
window is the visible part of the image in the raster's px; it is previewed only
while it covers at most 600 × 600 px, otherwise the canvas reads "Zoom in to
preview". A pan, a zoom or any setting change re-previews once it has been still
for 150 ms, and a newer change aborts the request in flight; the badge and its
errors sit in the canvas controls' polite status region. Previews run one at a
time on the preview thread, never on the event loop, and a request whose client
disconnected, or that a newer one superseded, stops at its next checkpoint. The
errors are 404, 409 for an unflattened or stale revision or a superseded request,
422 for the window or settings, and 503 when shutting down. The
server cleans the window plus a hidden 256 px margin (widening depends on
neighbours well beyond the window) of
the raster a Cleanup run of that revision starts from (before any earlier
cleanup), measuring every region of the whole raster once per revision so a
region the window cuts is judged by its whole area and width. It then replays
on the cleaned window the touch-ups a run would replay
(`touch_ups.replay_in_window`), judging an area recolour by its part inside the
cleaned crop, and never marks a replayed pixel as a change. The thread
holds the raster of one revision (with its measurements) and releases it after
10 minutes without a preview, so the web process gives the memory back; a
preview of another revision reloads and re-measures (about 6.4 s on `sourceup`). While the server is measuring, the badge
reads "Measuring regions of the whole image…"; the canvas learns
it from `GET /api/revisions/{id}/cleanup-preview/regions`, polled while the
request is in flight (see the [tracing specification](svg-tracing-spec.md)).
Nothing is saved; "Run
cleanup" still creates the revision. The differences from a full run (a removal
group split at the crop edge, the widen approximation, no protection-clearing
audit numbers), how often they showed in testing, and the per-update times (up
to about 1.3 s for widen on 1.5-megapixel subjects) are documented in the
[tracing specification](svg-tracing-spec.md).

## Stage invariants

- Palette inference decides which colours exist; it does not perform cutter
  cleanup.
- Segmentation decides which raster structures are AA and which are protected
  intentional features. Its result is persisted, validated, and consumed by
  tracing.
- Cutter cleanup is its own stage between editing and tracing. It is optional
  and operates on labels using physical or pixel thresholds; its defaults
  remain zero. One physical size governs both its thresholds and the traced
  SVG. The removed colour is excluded from cleanup and keeps its pixels.
- Tracing never cleans labels itself; it consumes the cleanup stage's labels,
  size and removed colour (the CLI runs the same cleanup inline first).
- Tracing is one `t3api.trace` call on the cleaned labels at the trace's
  half-width table, each curve kind's tube half-width, and its minimum sweeps,
  the least sweep a drawn circular or elliptical arc may have (the setting's
  `min_sweep` key, present only when either is above 0).
  Its planar map, `map.json`, stores every boundary edge once with the faces
  on its two sides, so adjacent colours share their boundary bit for bit; it is
  checked and read back before anything is built from it, and every later step
  (the edge SVG, the audit, a re-export, a save from nib) reads that file.
- Post-trace validation audits protected component survival on t3's painted
  labels. A changed explicit protection is a warning; nothing re-runs or
  repairs the trace.
- SVG writing does not infer geometry. Validation does not repair it.
