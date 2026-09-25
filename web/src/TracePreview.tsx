import { type ReactNode, useEffect, useState } from 'react'
import { LoaderCircle } from 'lucide-react'
import { errorMessage, type TraceSettings } from './api'
import type { ImageSize, ViewTransform } from './workspace'

// The live trace preview: the Trace canvas traces whatever is zoomed into, with the
// current (unsaved) trace form, and draws the shapes over the image at that spot.
// `useLivePreview` is the driver it shares with the Cleanup canvas's preview.

// The largest visible area (traced-image px) the server previews; kept equal to
// PREVIEW_MAX_AREA_PX in vinylgen/trace_window.py and CLEANUP_PREVIEW_MAX_AREA_PX in
// vinylgen/cleanup_window.py.
export const PREVIEW_MAX_AREA = 600 * 600
// A view or setting change re-previews once it has been still this long.
export const PREVIEW_DEBOUNCE_MS = 150
// While a preview is in flight, the canvas's busy note (why it is slow) is re-read this often.
export const BUSY_NOTE_POLL_MS = 250
export const BUSY_BADGE_TEXT = 'Updating live preview…'

export type PreviewWindow = { x: number; y: number; width: number; height: number }
export type TracePreviewResponse = {
  window: PreviewWindow; crop: PreviewWindow; image: ImageSize
  variant: 'traced-overlap' | 'traced-edge'; svg: string; elapsed_ms: number
}
// What the Trace canvas needs to preview: the revision a trace run would start from,
// the form's current settings, and the pixel grid the trace fits curves on.
export type LivePreviewOptions = PreviewSource<TraceSettings>

/**
 * The traced-image window the viewport currently shows, clamped to the image.
 * `stage` is the pixel grid the canvas lays the image out on; the traced grid may be
 * a multiple of it (a flatten upscale), so stage px are scaled into traced px.
 * Returns null when no part of the image is in view.
 */
export function visibleTraceWindow(transform: ViewTransform, viewport: ImageSize, stage: ImageSize, traced: ImageSize): PreviewWindow | null {
  if (!transform.scale || !viewport.width || !viewport.height || !stage.width || !stage.height || !traced.width || !traced.height) return null
  const kx = traced.width / stage.width
  const ky = traced.height / stage.height
  const left = Math.max(0, Math.floor(-transform.x / transform.scale * kx))
  const top = Math.max(0, Math.floor(-transform.y / transform.scale * ky))
  const right = Math.min(traced.width, Math.ceil((viewport.width - transform.x) / transform.scale * kx))
  const bottom = Math.min(traced.height, Math.ceil((viewport.height - transform.y) / transform.scale * ky))
  if (right <= left || bottom <= top) return null
  return { x: left, y: top, width: right - left, height: bottom - top }
}

export async function fetchTracePreview(revisionId: string, window: PreviewWindow, trace: TraceSettings, signal: AbortSignal): Promise<TracePreviewResponse> {
  const response = await fetch(`/api/revisions/${revisionId}/trace-preview`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
    body: JSON.stringify({ window, trace }),
  })
  if (!response.ok) {
    const text = await response.text()
    let invalidSettings = false
    try { invalidSettings = Array.isArray((JSON.parse(text) as { detail?: unknown }).detail) } catch { /* not JSON */ }
    // A proxy may replace the body with its own HTML page; errorMessage never shows it.
    throw new Error(invalidSettings ? 'The trace settings are not valid for a preview'
      : errorMessage(response.status, response.statusText, text))
  }
  return response.json() as Promise<TracePreviewResponse>
}

function svgUrl(svg: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
}

// The switch is a canvas preference: it survives stage changes and reloads.
export const LIVE_PREVIEW_STORAGE_KEY = 'vinylgen.livePreview'

export function storedSwitch(key: string, fallback = false): boolean {
  try {
    const value = localStorage.getItem(key)
    return value === null ? fallback : value === 'on'
  } catch { return fallback }
}

export function storeSwitch(key: string, on: boolean) {
  try { localStorage.setItem(key, on ? 'on' : 'off') } catch { /* storage unavailable */ }
}

// What a canvas previews: the revision, the form's current (unsaved) settings, and
// the pixel grid the previewed stage works on.
export type PreviewSource<Settings> = { revisionId: string; settings: Settings; tracedSize: ImageSize }
// A finished preview of `window`, placed on the stage by the canvas.
export type ShownPreview<Result> = { revisionId: string; window: PreviewWindow; result: Result }
export type LivePreviewState<Result> = {
  // The latest preview of the current revision, while it is the one to draw.
  shown: ShownPreview<Result> | null
  // The stage px per traced px, to place `shown.window` on the stage.
  kx: number; ky: number
  // The on-canvas switch, badge and status, with `extra` controls (always shown) after the switch.
  controls: (extra?: ReactNode) => ReactNode
  // Whether the canvas has a live preview with its switch on.
  enabled: boolean
}

// Reads, from the server, what the badge says while a preview of the revision is in
// flight: a note on why it is slow, or null for the plain "Updating live preview…".
export type BusyNote = (revisionId: string, signal: AbortSignal) => Promise<string | null>

/**
 * The shared driver of a canvas's live preview: it previews whatever is zoomed into,
 * ~150 ms after the view or the settings settle, aborting the request in flight when
 * a newer change arrives, and renders the switch (saved under `storageKey`), the
 * "Live preview" badge, "Zoom in to preview" and errors. With `busyNote`, the busy
 * badge reads the note it returns, read when the request starts and every
 * `BUSY_NOTE_POLL_MS` until it settles. Without `options` it requests and renders
 * nothing.
 */
export function useLivePreview<Settings, Result>(
  options: PreviewSource<Settings> | undefined, transform: ViewTransform, viewport: ImageSize, stage: ImageSize,
  { storageKey, label, request, busyNote }: { storageKey: string; label: string; request: (revisionId: string, window: PreviewWindow, settings: Settings, signal: AbortSignal) => Promise<{ window: PreviewWindow; result: Result }>; busyNote?: BusyNote },
): LivePreviewState<Result> {
  const [enabled, setEnabled] = useState(() => storedSwitch(storageKey))
  const [shown, setShown] = useState<ShownPreview<Result> | null>(null)
  const [busy, setBusy] = useState(false)
  // The server's latest note on the request in flight, for the revision it was read for.
  // A superseded request leaves it standing (the server may still be doing that work)
  // until the next request's first read replaces it.
  const [note, setNote] = useState<{ revisionId: string; text: string } | null>(null)
  const [error, setError] = useState('')
  const view = options ? visibleTraceWindow(transform, viewport, stage, options.tracedSize) : null
  const tooLarge = Boolean(view && view.width * view.height > PREVIEW_MAX_AREA)
  const revisionId = options?.revisionId ?? null
  // One key for everything a preview depends on, so any change (a settled pan or zoom,
  // any setting) re-previews, and a newer change aborts the request in flight.
  const requestKey = enabled && options && view && !tooLarge ? JSON.stringify([revisionId, view, options.settings]) : null

  useEffect(() => {
    if (!requestKey || !options || !view) {
      setBusy(false)
      setNote(null)
      // "Zoom in to preview", or a view off the image, replaces any earlier failure:
      // it belonged to another view.
      if (tooLarge || (options && !view)) setError('')
      return
    }
    const controller = new AbortController()
    let settled = false
    let noteTimer: ReturnType<typeof setTimeout> | undefined
    const readNote = () => {
      if (!busyNote) return
      const revision = options.revisionId
      busyNote(revision, controller.signal)
        .then(text => { if (!settled && !controller.signal.aborted) setNote(text ? { revisionId: revision, text } : null) })
        .catch(() => { /* an unreadable note leaves the badge as it is */ })
        .finally(() => { if (!settled && !controller.signal.aborted) noteTimer = setTimeout(readNote, BUSY_NOTE_POLL_MS) })
    }
    const timer = setTimeout(() => {
      // A new request supersedes the failure of the last one.
      setBusy(true)
      setError('')
      readNote()
      request(options.revisionId, view, options.settings, controller.signal)
        .then(body => {
          if (controller.signal.aborted) return
          setShown({ revisionId: options.revisionId, window: body.window, result: body.result })
          setError('')
        })
        .catch((cause: unknown) => {
          if (controller.signal.aborted) return
          // The last preview came from other settings or another view; never leave
          // it standing under the error as if it were the current result.
          setShown(null)
          setError(cause instanceof Error ? cause.message : String(cause))
        })
        .finally(() => {
          if (controller.signal.aborted) return
          settled = true
          clearTimeout(noteTimer)
          setBusy(false)
          setNote(null)
        })
    }, PREVIEW_DEBOUNCE_MS)
    return () => { clearTimeout(timer); clearTimeout(noteTimer); controller.abort() }
    // requestKey captures options.revisionId, options.settings and view by value.
  }, [requestKey, tooLarge, view === null])

  const visibleShown = options && enabled && !tooLarge && !error && shown && shown.revisionId === revisionId ? shown : null
  const kx = options ? stage.width / options.tracedSize.width : 1
  const ky = options ? stage.height / options.tracedSize.height : 1
  function toggle(on: boolean) {
    setEnabled(on)
    if (!on) setError('')
    storeSwitch(storageKey, on)
  }
  // Only the switches take pointer input; everywhere else a pan can start. The badge
  // is visual only while idle; the polite status region announces it ("Updating live
  // preview…" or the server's note) while busy, plus "Zoom in to preview" and errors —
  // not the idle "Live preview" label, which would otherwise be announced after every
  // settle with nothing meaningful to say.
  const busyText = note && note.revisionId === revisionId ? note.text : BUSY_BADGE_TEXT
  const controls = (extra?: ReactNode) => options ? <div className="trace-preview-controls" role="group" aria-label={label}>
    <label className="trace-preview-switch" onPointerDown={event => event.stopPropagation()}><input type="checkbox" role="switch" checked={enabled} onChange={event => toggle(event.target.checked)} />Live preview</label>
    {extra}
    {enabled && !tooLarge && !busy && visibleShown && <p className="trace-preview-badge" aria-hidden="true">Live preview</p>}
    <div className="trace-preview-announcer" role="status" aria-live="polite">
      {enabled && !tooLarge && busy && <p className="trace-preview-badge is-busy"><LoaderCircle aria-hidden="true" />{busyText}</p>}
      {enabled && tooLarge && <p className="trace-preview-status">Zoom in to preview</p>}
      {enabled && !tooLarge && error && <p className="trace-preview-status error">Preview failed: {error}</p>}
    </div>
  </div> : null
  return { shown: visibleShown, kx, ky, controls, enabled: Boolean(options) && enabled }
}

// Where a preview of `window` (traced px) sits on the stage.
export function stagePlacement(window: PreviewWindow, kx: number, ky: number) {
  return { left: window.x * kx, top: window.y * ky, width: window.width * kx, height: window.height * ky }
}

async function requestTracePreview(revisionId: string, window: PreviewWindow, trace: TraceSettings, signal: AbortSignal) {
  const body = await fetchTracePreview(revisionId, window, trace, signal)
  return { window: body.window, result: { url: svgUrl(body.svg), svg: body.svg } }
}

/**
 * Drives the Trace canvas's live preview. Returns the shapes to place inside the
 * transformed image stage (so they track pan and zoom exactly), the on-canvas
 * controls (the switch, the "Live preview" badge, busy and error states), and the
 * shown preview's SVG text and window with the stage px per traced px, and whether
 * its switch is on. Without `options` (any canvas but Trace) it renders nothing and
 * requests nothing.
 */
export function useTracePreview(options: LivePreviewOptions | undefined, transform: ViewTransform, viewport: ImageSize, stage: ImageSize): { stageLayer: ReactNode; controls: ReactNode; shown: { window: PreviewWindow; svg: string } | null; kx: number; ky: number; enabled: boolean } {
  const preview = useLivePreview(options, transform, viewport, stage,
    { storageKey: LIVE_PREVIEW_STORAGE_KEY, label: 'Live trace preview', request: requestTracePreview })
  const { shown } = preview
  const stageLayer = shown ? <img
    className="trace-preview-shapes vector"
    src={shown.result.url}
    alt="Live trace preview"
    draggable={false}
    data-window={`${shown.window.x} ${shown.window.y} ${shown.window.width} ${shown.window.height}`}
    style={stagePlacement(shown.window, preview.kx, preview.ky)}
  /> : null
  // The shown preview's SVG and window, for overlays drawn from its geometry.
  return { stageLayer, controls: preview.controls(), shown: shown ? { window: shown.window, svg: shown.result.svg } : null, kx: preview.kx, ky: preview.ky, enabled: preview.enabled }
}
