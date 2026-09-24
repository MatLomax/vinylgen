import { type ReactNode, useState } from 'react'
import { errorMessage, type CleanupSettings } from './api'
import { stagePlacement, storeSwitch, storedSwitch, useLivePreview, type PreviewSource, type PreviewWindow } from './TracePreview'
import type { ImageSize, ViewTransform } from './workspace'

// The live cleanup preview: the Cleanup canvas cleans whatever is zoomed into, with
// the current (unsaved) Cleanup form, and draws the cleaned pixels over the image at
// that spot, with the changes (removed red, widened green) as a toggleable overlay.

export type CleanupPreviewResponse = {
  window: PreviewWindow; crop: PreviewWindow; image: ImageSize
  pixels: string; changes: string; removed_pixels: number; widened_pixels: number; elapsed_ms: number
}
// What the Cleanup canvas needs to preview: the revision a Cleanup run would start
// from, the form's current settings (physical size included), and the raster's grid.
export type CleanupPreviewOptions = PreviewSource<CleanupSettings>
type CleanupResult = { pixels: string; changes: string }

// Each switch is a canvas preference of its own: it survives stage changes and reloads.
export const CLEANUP_PREVIEW_STORAGE_KEY = 'vinylgen.cleanupLivePreview'
export const CLEANUP_CHANGES_STORAGE_KEY = 'vinylgen.cleanupPreviewChanges'

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

async function requestCleanupPreview(revisionId: string, window: PreviewWindow, cleanup: CleanupSettings, signal: AbortSignal) {
  const body = await fetchCleanupPreview(revisionId, window, cleanup, signal)
  return { window: body.window, result: { pixels: body.pixels, changes: body.changes } }
}

/**
 * Drives the Cleanup canvas's live preview. Returns the cleaned pixels and the
 * changes overlay to place inside the transformed image stage, both on the window's
 * raster px so they register exactly, and the on-canvas controls: the switch, the
 * "Show changes" toggle, the badge, busy and error states. Without `options` (any
 * canvas but Cleanup) it renders nothing and requests nothing.
 */
export function useCleanupPreview(options: CleanupPreviewOptions | undefined, transform: ViewTransform, viewport: ImageSize, stage: ImageSize): { stageLayer: ReactNode; controls: ReactNode } {
  const preview = useLivePreview(options, transform, viewport, stage,
    { storageKey: CLEANUP_PREVIEW_STORAGE_KEY, label: 'Live cleanup preview', request: requestCleanupPreview })
  const [showChanges, setShowChanges] = useState(() => storedSwitch(CLEANUP_CHANGES_STORAGE_KEY, true))
  const { shown } = preview
  const placement = shown ? stagePlacement(shown.window, preview.kx, preview.ky) : undefined
  const at = shown ? `${shown.window.x} ${shown.window.y} ${shown.window.width} ${shown.window.height}` : undefined
  const stageLayer = shown ? <>
    <img className="cleanup-preview-pixels" src={shown.result.pixels} alt="Live cleanup preview" draggable={false} data-window={at} style={placement} />
    {showChanges && <img className="cleanup-preview-changes" src={shown.result.changes} alt="Cleanup changes: removed in red, widened in green" draggable={false} data-window={at} style={placement} />}
  </> : null
  function toggleChanges(on: boolean) {
    setShowChanges(on)
    storeSwitch(CLEANUP_CHANGES_STORAGE_KEY, on)
  }
  const changesToggle = <label className="trace-preview-switch" onPointerDown={event => event.stopPropagation()}><input type="checkbox" role="switch" checked={showChanges} onChange={event => toggleChanges(event.target.checked)} />Show changes</label>
  return { stageLayer, controls: preview.controls(changesToggle) }
}
