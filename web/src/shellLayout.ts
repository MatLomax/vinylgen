import { useEffect, useState } from 'react'

// Page-level layout keys off the window: the stage rail and the stage settings panel are
// owned by the page. Each query matches the narrower side, so a window where matchMedia
// is unavailable lays out as a wide desktop.
export const PHONE_QUERY = '(max-width: 700px)'
export const OVERLAY_PANEL_QUERY = '(max-width: 1024px)'
export const ICON_RAIL_QUERY = '(max-width: 1279px)'

export const STAGE_RAIL_STORAGE_KEY = 'vinylgen.stageRail'
export const SETTINGS_PANEL_STORAGE_KEY = 'vinylgen.settingsPanel'

/**
 * How the stage settings panel sits beside the canvas:
 * - `push`: a column beside the canvas (above 1024px), collapsible to a thin edge;
 * - `overlay`: over the right of the canvas (701px to 1024px), behind a thin edge while closed;
 * - `drawer`: a bottom drawer under a phone's canvas;
 * - `page`: the whole phone surface, on stages without a canvas.
 */
export type PanelMode = 'push' | 'overlay' | 'drawer' | 'page'

export function panelModeFor(phone: boolean, overlay: boolean, canvasShown: boolean): PanelMode {
  if (phone) return canvasShown ? 'drawer' : 'page'
  return overlay ? 'overlay' : 'push'
}

function matches(query: string) {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(query).matches
}

export function useMediaQuery(query: string) {
  const [matched, setMatched] = useState(() => matches(query))
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const list = window.matchMedia(query)
    setMatched(list.matches)
    const changed = (event: MediaQueryListEvent) => setMatched(event.matches)
    list.addEventListener?.('change', changed)
    return () => list.removeEventListener?.('change', changed)
  }, [query])
  return matched
}

/** A remembered two-way choice, or null while the user has not made one. */
export function storedChoice<T extends string>(key: string, values: readonly T[]): T | null {
  try {
    const value = localStorage.getItem(key)
    return values.includes(value as T) ? value as T : null
  } catch { return null }
}

export function storeChoice(key: string, value: string) {
  try { localStorage.setItem(key, value) } catch { /* storage unavailable */ }
}
