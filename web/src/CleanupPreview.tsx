import { type ReactNode, useId, useState } from 'react'
import { errorMessage, type CleanupSettings } from './api'
import { stagePlacement, storeSwitch, storedSwitch, useLivePreview, type PreviewSource, type PreviewWindow } from './TracePreview'
import type { ImageSize, ViewTransform } from './workspace'

// The live cleanup preview: the Cleanup canvas cleans whatever is zoomed into, with
// the current (unsaved) Cleanup form, and draws the cleaned pixels over the image at
// that spot, with what a Cleanup run would change on the shown raster on top. The
// canvas's "Show changes" switch draws what the shown revision's Cleanup run has
// changed, over the whole image, with or without the preview.

export type CleanupPreviewResponse = {
  window: PreviewWindow; crop: PreviewWindow; image: ImageSize
  pixels: string; changes: string; removed_pixels: number; widened_pixels: number; returned_pixels: number; elapsed_ms: number
}
// What the Cleanup canvas needs to preview: the revision a Cleanup run would start
// from, the form's current settings (physical size included), and the raster's grid;
// and why "Show changes" has nothing to show when the revision has no changes overlay.
export type CleanupPreviewOptions = PreviewSource<CleanupSettings> & { changesUnavailable?: string }
type CleanupResult = { pixels: string; changes: string }

// Each switch is a canvas preference of its own: it survives stage changes and reloads.
// "Show changes" on draws the applied-changes overlay.
export const CLEANUP_PREVIEW_STORAGE_KEY = 'vinylgen.cleanupLivePreview'
export const CLEANUP_CHANGES_STORAGE_KEY = 'vinylgen.cleanupShowChanges'
// The artifact a Cleanup run records of the pixels it changed (removed red, widened green).
export const CLEANUP_CHANGES_OVERLAY_KIND = 'cleanup-changes-overlay'

export async function fetchCleanupPreview(revisionId: string, window: PreviewWindow, cleanup: CleanupSettings, signal: AbortSignal): Promise<CleanupPreviewResponse> {
  const response = await fetch(`/api/revisions/${revisionId}/cleanup-preview`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
    body: JSON.stringify({ window, cleanup }),
  })
  if (!response.ok) {
    const text = await response.text()
    let invalidSettings = false
    try { invalidSettings = Array.isArray((JSON.parse(text) as { detail?: unknown }).detail) } catch { /* not JSON */ }
    // A proxy may replace the body with its own HTML page; errorMessage never shows it.
    throw new Error(invalidSettings ? 'The cleanup settings are not valid for a preview'
      : errorMessage(response.status, response.statusText, text))
  }
  return response.json() as Promise<CleanupPreviewResponse>
}

// Whether the preview thread holds the whole raster's region measurements a cleanup
// preview of the revision cleans with, and whether it is measuring them now.
export type CleanupRegionsState = { measured: boolean; measuring: boolean }
export const MEASURING_BADGE_TEXT = 'Measuring regions of the whole image…'

export async function fetchCleanupRegionsState(revisionId: string, signal: AbortSignal): Promise<CleanupRegionsState> {
  const response = await fetch(`/api/revisions/${revisionId}/cleanup-preview/regions`, { signal })
  if (!response.ok) throw new Error(errorMessage(response.status, response.statusText, await response.text()))
  return response.json() as Promise<CleanupRegionsState>
}

// The busy badge says so while the server measures the revision's regions, the slow
// first step of a revision's first preview (and of every return from the Trace canvas,
// whose preview releases them).
async function measuringNote(revisionId: string, signal: AbortSignal) {
  const state = await fetchCleanupRegionsState(revisionId, signal)
  return state.measuring ? MEASURING_BADGE_TEXT : null
}

async function requestCleanupPreview(revisionId: string, window: PreviewWindow, cleanup: CleanupSettings, signal: AbortSignal) {
  const body = await fetchCleanupPreview(revisionId, window, cleanup, signal)
  return { window: body.window, result: { pixels: body.pixels, changes: body.changes } }
}

// The "Show changes" switch: its state, whether the revision has a changes overlay to
// draw, and whether the canvas is drawing it now.
export type ShowChanges = { on: boolean; set: (on: boolean) => void; available: boolean; drawn: boolean }

/**
 * Drives the Cleanup canvas's live preview. Returns the cleaned pixels and what a
 * Cleanup run would change (removed red, widened green, returned to the uncleaned
 * colour blue), both on the window's raster px inside the transformed image stage so
 * they register exactly, and the on-canvas controls: the Live preview switch, the
 * "Show changes" switch (`showChanges`, which the canvas applies to the revision's
 * changes overlay; disabled with the reason when there is none, and shown while the
 * preview is paused too, with `pausedStatus` below it), a legend line for each
 * overlay on view, and the badge (which reads "Measuring regions…" while the server
 * measures the revision's regions), busy and error states. Without `options` (any
 * canvas but Cleanup) it renders nothing and requests nothing.
 */
export function useCleanupPreview(options: CleanupPreviewOptions | undefined, transform: ViewTransform, viewport: ImageSize, stage: ImageSize, showChanges: ShowChanges, paused: boolean, pausedStatus?: string): { stageLayer: ReactNode; controls: ReactNode } {
  const preview = useLivePreview(options, transform, viewport, stage,
    { storageKey: CLEANUP_PREVIEW_STORAGE_KEY, label: 'Live cleanup preview', request: requestCleanupPreview, busyNote: measuringNote })
  const reasonId = useId()
  const { shown } = preview
  const placement = shown ? stagePlacement(shown.window, preview.kx, preview.ky) : undefined
  const at = shown ? `${shown.window.x} ${shown.window.y} ${shown.window.width} ${shown.window.height}` : undefined
  const stageLayer = shown ? <>
    <img className="cleanup-preview-pixels" src={shown.result.pixels} alt="Live cleanup preview" draggable={false} data-window={at} style={placement} />
    <img className="cleanup-preview-changes" src={shown.result.changes} alt="What Cleanup would change: removed in red, widened in green, returned in blue" draggable={false} data-window={at} style={placement} />
  </> : null
  const unavailable = showChanges.available ? null : options?.changesUnavailable ?? 'No Cleanup changes to show'
  // Red and green mean "would change" inside the live window and "changed" outside it,
  // so each overlay on view gets its own line.
  const liveShown = Boolean(shown) && !paused
  const legend = liveShown || showChanges.drawn ? <ul className="cleanup-preview-legend" aria-label="Overlay colours">
    {liveShown && <li>Live: red would be removed, green widened, blue restored</li>}
    {showChanges.drawn && <li>Show changes: red removed, green widened by the last Cleanup</li>}
  </ul> : null
  const extra = <>
    <label className="trace-preview-switch" onPointerDown={event => event.stopPropagation()}><input type="checkbox" role="switch" checked={showChanges.on && showChanges.available} disabled={!showChanges.available} aria-describedby={unavailable ? reasonId : undefined} onChange={event => showChanges.set(event.target.checked)} />Show changes</label>
    {unavailable && <p id={reasonId} className="trace-preview-status">{unavailable}</p>}
    {legend}
    <div aria-live="polite">{paused && pausedStatus && <p className="trace-preview-status error">{pausedStatus}</p>}</div>
  </>
  return { stageLayer, controls: preview.controls(extra) }
}

// The "Show changes" preference, remembered across stage changes and reloads.
export function useShowChangesSwitch(): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState(() => storedSwitch(CLEANUP_CHANGES_STORAGE_KEY, true))
  return [on, (next: boolean) => { setOn(next); storeSwitch(CLEANUP_CHANGES_STORAGE_KEY, next) }]
}
