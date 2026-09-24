import { type ReactNode, useEffect, useState } from 'react'
import { LoaderCircle } from 'lucide-react'
import { errorMessage, type TraceSettings } from './api'
import type { ImageSize, ViewTransform } from './workspace'

// The live trace preview: the Trace canvas traces whatever is zoomed into, with the
// current (unsaved) trace form, and draws the shapes over the image at that spot.

// The largest visible area (traced-image px) the server previews; kept equal to
// PREVIEW_MAX_AREA_PX in vinylgen/trace_window.py.
export const PREVIEW_MAX_AREA = 600 * 600
// A view or setting change re-previews once it has been still this long.
export const PREVIEW_DEBOUNCE_MS = 150

export type PreviewWindow = { x: number; y: number; width: number; height: number }
export type TracePreviewResponse = {
  window: PreviewWindow; crop: PreviewWindow; image: ImageSize
  variant: 'traced-overlap' | 'traced-edge'; svg: string; elapsed_ms: number
}
// What the Trace canvas needs to preview: the revision a trace run would start from,
// the form's current settings, and the pixel grid the trace fits curves on.
export type LivePreviewOptions = { revisionId: string; settings: TraceSettings; tracedSize: ImageSize }

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

type Shown = { revisionId: string; window: PreviewWindow; url: string }

// The switch is a canvas preference: it survives stage changes and reloads.
export const LIVE_PREVIEW_STORAGE_KEY = 'vinylgen.livePreview'

function storedEnabled(): boolean {
  try { return localStorage.getItem(LIVE_PREVIEW_STORAGE_KEY) === 'on' } catch { return false }
}

/**
 * Drives the Trace canvas's live preview. Returns the shapes to place inside the
 * transformed image stage (so they track pan and zoom exactly) and the on-canvas
 * controls: the switch, the "Live preview" badge, busy and error states. Without
 * `options` (any canvas but Trace) it renders nothing and requests nothing.
 */
export function useTracePreview(options: LivePreviewOptions | undefined, transform: ViewTransform, viewport: ImageSize, stage: ImageSize): { stageLayer: ReactNode; controls: ReactNode } {
  const [enabled, setEnabled] = useState(storedEnabled)
  const [shown, setShown] = useState<Shown | null>(null)
  const [busy, setBusy] = useState(false)
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
      // "Zoom in to preview", or a view off the image, replaces any earlier failure:
      // it belonged to another view.
      if (tooLarge || (options && !view)) setError('')
      return
    }
    const controller = new AbortController()
    const timer = setTimeout(() => {
      // A new request supersedes the failure of the last one.
      setBusy(true)
      setError('')
      fetchTracePreview(options.revisionId, view, options.settings, controller.signal)
        .then(body => {
          if (controller.signal.aborted) return
          setShown({ revisionId: options.revisionId, window: body.window, url: svgUrl(body.svg) })
          setError('')
        })
        .catch((cause: unknown) => {
          if (controller.signal.aborted) return
          // The last shapes came from other settings or another view; never leave
          // them standing under the error as if they were the current result.
          setShown(null)
          setError(cause instanceof Error ? cause.message : String(cause))
        })
        .finally(() => { if (!controller.signal.aborted) setBusy(false) })
    }, PREVIEW_DEBOUNCE_MS)
    return () => { clearTimeout(timer); controller.abort() }
    // requestKey captures options.revisionId, options.settings and view by value.
  }, [requestKey, tooLarge, view === null])

  if (!options) return { stageLayer: null, controls: null }
  const visibleShown = enabled && !tooLarge && !error && shown && shown.revisionId === revisionId ? shown : null
  const kx = stage.width / options.tracedSize.width
  const ky = stage.height / options.tracedSize.height
  const stageLayer = visibleShown ? <img
    className="trace-preview-shapes vector"
    src={visibleShown.url}
    alt="Live trace preview"
    draggable={false}
    data-window={`${visibleShown.window.x} ${visibleShown.window.y} ${visibleShown.window.width} ${visibleShown.window.height}`}
    style={{ left: visibleShown.window.x * kx, top: visibleShown.window.y * ky, width: visibleShown.window.width * kx, height: visibleShown.window.height * ky }}
  /> : null
  function toggle(on: boolean) {
    setEnabled(on)
    if (!on) setError('')
    try { localStorage.setItem(LIVE_PREVIEW_STORAGE_KEY, on ? 'on' : 'off') } catch { /* storage unavailable */ }
  }
  // Only the switch takes pointer input; everywhere else a pan can start. The badge
  // is visual only; the polite status region announces "Zoom in to preview" and
  // errors, not every settle.
  const controls = <div className="trace-preview-controls" role="group" aria-label="Live trace preview">
    <label className="trace-preview-switch" onPointerDown={event => event.stopPropagation()}><input type="checkbox" role="switch" checked={enabled} onChange={event => toggle(event.target.checked)} />Live preview</label>
    {enabled && !tooLarge && (visibleShown || busy) && <p className={`trace-preview-badge ${busy ? 'is-busy' : ''}`} aria-hidden="true">{busy && <LoaderCircle aria-hidden="true" />}{busy ? 'Updating live preview…' : 'Live preview'}</p>}
    <div className="trace-preview-announcer" role="status" aria-live="polite">
      {enabled && tooLarge && <p className="trace-preview-status">Zoom in to preview</p>}
      {enabled && !tooLarge && error && <p className="trace-preview-status error">Preview failed: {error}</p>}
    </div>
  </div>
  return { stageLayer, controls }
}
