export type Rgb = readonly [number, number, number]
export type Pixels = { data: Uint8ClampedArray; width: number; height: number }
export type PaletteEntry = { index: number; rgb: Rgb }
// A selected component's mask is cropped to its own bounding box, so N selected
// regions cost the sum of their areas rather than N full-image masks, and every
// overlay/neighbour pass touches only the affected pixels.
export type Bounds = { x: number; y: number; width: number; height: number }
export type Component = { point: [number, number]; rgb: Rgb; label: number; bounds: Bounds; mask: Uint8Array }
// A patch of overlay pixels confined to `bounds` in image-pixel coordinates; the
// screen-space canvas layer blits just this rectangle, scaled by the current zoom.
export type OverlayRegion = { bounds: Bounds; data: Uint8ClampedArray }
export type OverlayFrame = OverlayRegion

function colourAt(pixels: Pixels, x: number, y: number): Rgb {
  const offset = (y * pixels.width + x) * 4
  return [pixels.data[offset], pixels.data[offset + 1], pixels.data[offset + 2]]
}

function matches(pixels: Pixels, x: number, y: number, rgb: Rgb): boolean {
  const offset = (y * pixels.width + x) * 4
  return pixels.data[offset] === rgb[0] && pixels.data[offset + 1] === rgb[1] && pixels.data[offset + 2] === rgb[2]
}

function withinBounds(bounds: Bounds, x: number, y: number): boolean {
  return x >= bounds.x && y >= bounds.y && x < bounds.x + bounds.width && y < bounds.y + bounds.height
}

function maskCovers(component: Component, x: number, y: number): boolean {
  const { bounds, mask } = component
  return withinBounds(bounds, x, y) && mask[(y - bounds.y) * bounds.width + (x - bounds.x)] === 1
}

// Parse a "#rrggbb" (or shorthand "#rgb") colour into an RGB triple. Values the
// colour picker emits are always full six-digit hex; shorthand is accepted so
// hand-typed hex still previews.
export function hexToRgb(hex: string): Rgb {
  const value = hex.replace('#', '')
  const full = value.length === 3 ? value.replace(/./g, char => char + char) : value.padEnd(6, '0').slice(0, 6)
  const int = parseInt(full, 16) || 0
  return [(int >> 16) & 255, (int >> 8) & 255, int & 255]
}

// A full opaque RGBA copy of the flattened raster with one palette colour swapped
// to a new RGB, for live-previewing a recolour before it is committed. Matching by
// exact RGB mirrors labelForRgb: a flattened raster holds only exact palette colours.
export function recolourPreviewPixels(pixels: Pixels, from: Rgb, to: Rgb): Uint8ClampedArray {
  const source = pixels.data
  const preview = new Uint8ClampedArray(source)
  for (let offset = 0; offset < source.length; offset += 4) {
    if (source[offset] === from[0] && source[offset + 1] === from[1] && source[offset + 2] === from[2]) {
      preview[offset] = to[0]; preview[offset + 1] = to[1]; preview[offset + 2] = to[2]
    }
  }
  return preview
}

export function labelForRgb(rgb: Rgb, palette: PaletteEntry[]): number {
  const found = palette.find(entry => entry.rgb[0] === rgb[0] && entry.rgb[1] === rgb[1] && entry.rgb[2] === rgb[2])
  return found ? found.index : -1
}

// The four-connected component of identically-coloured pixels containing (x, y),
// with its mask cropped to the component's bounding box. Matching by exact RGB
// mirrors the authoritative label component the backend recomputes on commit,
// because a flattened raster holds only exact palette colours.
export function floodComponent(pixels: Pixels, x: number, y: number, palette: PaletteEntry[]): Component | null {
  const { width, height } = pixels
  if (x < 0 || y < 0 || x >= width || y >= height) return null
  const rgb = colourAt(pixels, x, y)
  // A transient full-image visited grid drives the flood; the stored mask is cropped
  // to the region afterwards, so nothing full-image survives past this call.
  const visited = new Uint8Array(width * height)
  const start = y * width + x
  visited[start] = 1
  let minX = x, maxX = x, minY = y, maxY = y
  const stack = [start]
  while (stack.length) {
    const cell = stack.pop()!
    const cx = cell % width
    const cy = (cell - cx) / width
    if (cx < minX) minX = cx
    if (cx > maxX) maxX = cx
    if (cy < minY) minY = cy
    if (cy > maxY) maxY = cy
    const neighbours: Array<[number, number]> = [[cx - 1, cy], [cx + 1, cy], [cx, cy - 1], [cx, cy + 1]]
    for (const [nx, ny] of neighbours) {
      if (nx < 0 || ny < 0 || nx >= width || ny >= height) continue
      const cellNeighbour = ny * width + nx
      if (!visited[cellNeighbour] && matches(pixels, nx, ny, rgb)) {
        visited[cellNeighbour] = 1
        stack.push(cellNeighbour)
      }
    }
  }
  const bounds: Bounds = { x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1 }
  const mask = new Uint8Array(bounds.width * bounds.height)
  for (let yy = minY; yy <= maxY; yy++) {
    for (let xx = minX; xx <= maxX; xx++) {
      if (visited[yy * width + xx]) mask[(yy - minY) * bounds.width + (xx - minX)] = 1
    }
  }
  return { point: [x, y], rgb, label: labelForRgb(rgb, palette), bounds, mask }
}

export function componentIndexAt(selection: Component[], x: number, y: number): number {
  return selection.findIndex(component => maskCovers(component, x, y))
}

// Add the component under (x, y) to the selection, or remove it if already selected.
export function toggleComponent(selection: Component[], pixels: Pixels, x: number, y: number, palette: PaletteEntry[]): Component[] {
  const existing = componentIndexAt(selection, x, y)
  if (existing >= 0) return selection.filter((_, index) => index !== existing)
  const component = floodComponent(pixels, x, y, palette)
  return component ? [...selection, component] : selection
}

// Drop a specific batch of components from the selection by identity. Used after a
// region replace lands to deselect exactly the areas that were submitted, while any
// area clicked after kickoff — a distinct component object even for the same point —
// stays selected for the next edit.
export function removeComponents(selection: Component[], removed: Component[]): Component[] {
  return selection.filter(component => !removed.includes(component))
}

// The surviving selection a region edit hands to the revision it produced, so the
// reload keeps areas clicked after the edit was kicked off. `applied` records whether
// the (two-run) revision→artifacts reload has already restored it.
export type RegionReloadHandoff = { id: string; selection: Component[]; applied: boolean }

// Decide what the region selection becomes when a revision loads. A region edit bumps
// the revision, whose reload fires the selection effect twice — once when the revision
// id changes and again when the reloaded flattened image settles. Restore the surviving
// selection on the first matching run ('apply'), leave whatever is selected untouched on
// the second ('keep' — the user may have clicked more between the runs), and clear the
// selection for any other revision change ('clear').
export function regionReloadDecision(handoff: RegionReloadHandoff | null, revision: string | null):
  | { action: 'apply'; selection: Component[]; handoff: RegionReloadHandoff }
  | { action: 'keep' }
  | { action: 'clear' } {
  if (handoff && handoff.id === revision) {
    if (!handoff.applied) return { action: 'apply', selection: handoff.selection, handoff: { ...handoff, applied: true } }
    return { action: 'keep' }
  }
  return { action: 'clear' }
}

// The tight bounding box enclosing every selected component, or null when nothing is
// selected. Every overlay/neighbour pass is confined to this box.
export function selectionBounds(selection: Component[]): Bounds | null {
  if (!selection.length) return null
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const { bounds } of selection) {
    if (bounds.x < minX) minX = bounds.x
    if (bounds.y < minY) minY = bounds.y
    if (bounds.x + bounds.width - 1 > maxX) maxX = bounds.x + bounds.width - 1
    if (bounds.y + bounds.height - 1 > maxY) maxY = bounds.y + bounds.height - 1
  }
  return { x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1 }
}

// The union of the selection's masks, cropped to the selection's bounding box. Cost is
// the sum of the component areas, never the whole image.
export function combinedMask(selection: Component[]): { bounds: Bounds; mask: Uint8Array } | null {
  const bounds = selectionBounds(selection)
  if (!bounds) return null
  const mask = new Uint8Array(bounds.width * bounds.height)
  for (const component of selection) {
    const inner = component.bounds
    for (let yy = 0; yy < inner.height; yy++) {
      for (let xx = 0; xx < inner.width; xx++) {
        if (!component.mask[yy * inner.width + xx]) continue
        const gx = inner.x + xx - bounds.x
        const gy = inner.y + yy - bounds.y
        mask[gy * bounds.width + gx] = 1
      }
    }
  }
  return { bounds, mask }
}

export function selectionLabels(selection: Component[]): number[] {
  return selection.map(component => component.label)
}

// Palette indices that border the selection but are not themselves selected.
// Applying one of these is a "dissolve into a surrounding colour" rather than a
// free recolour. Only the selection's bounding box is scanned; neighbours are read
// from the full raster so a border pixel just outside the box is still classified.
export function neighbourLabels(selection: Component[], pixels: Pixels, palette: PaletteEntry[]): number[] {
  const combined = combinedMask(selection)
  if (!combined) return []
  const { bounds, mask } = combined
  const { width, height } = pixels
  const selected = new Set(selectionLabels(selection))
  const labels = new Set<number>()
  for (let ly = 0; ly < bounds.height; ly++) {
    for (let lx = 0; lx < bounds.width; lx++) {
      if (!mask[ly * bounds.width + lx]) continue
      const x = bounds.x + lx, y = bounds.y + ly
      const neighbours: Array<[number, number]> = [[x - 1, y], [x + 1, y], [x, y - 1], [x, y + 1]]
      for (const [nx, ny] of neighbours) {
        if (nx < 0 || ny < 0 || nx >= width || ny >= height) continue
        if (withinBounds(bounds, nx, ny) && mask[(ny - bounds.y) * bounds.width + (nx - bounds.x)]) continue
        const label = labelForRgb(colourAt(pixels, nx, ny), palette)
        if (label >= 0 && !selected.has(label)) labels.add(label)
      }
    }
  }
  return [...labels]
}

// RGBA overlay for staged pencil cells, cropped to the cells' bounding box: each cell
// is painted its target palette colour at full opacity so an uncommitted stroke
// previews exactly as it will flatten. `painted` maps a full-image cell (y*width + x)
// to the palette label to paint there.
export function paintOverlayPixels(painted: Map<number, number>, palette: PaletteEntry[], width: number): OverlayRegion | null {
  if (!painted.size) return null
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const cell of painted.keys()) {
    const x = cell % width, y = (cell - x) / width
    if (x < minX) minX = x
    if (x > maxX) maxX = x
    if (y < minY) minY = y
    if (y > maxY) maxY = y
  }
  const bounds: Bounds = { x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1 }
  const data = new Uint8ClampedArray(bounds.width * bounds.height * 4)
  const rgbByLabel = new Map(palette.map(entry => [entry.index, entry.rgb]))
  for (const [cell, label] of painted) {
    const rgb = rgbByLabel.get(label)
    if (!rgb) continue
    const x = cell % width, y = (cell - x) / width
    const offset = ((y - minY) * bounds.width + (x - minX)) * 4
    data[offset] = rgb[0]; data[offset + 1] = rgb[1]; data[offset + 2] = rgb[2]; data[offset + 3] = 255
  }
  return { bounds, data }
}

// Whether two palette entries share an identical RGB. When they do, an RGB-based flood
// can span more than one label, so a single reassign_region (which the server resolves
// by label) recolours only a subset of the flooded blob — and the client must not
// optimistically adopt a raster it painted for the whole blob.
export function paletteHasDuplicateRgb(palette: PaletteEntry[]): boolean {
  const seen = new Set<string>()
  for (const { rgb } of palette) {
    const key = `${rgb[0]},${rgb[1]},${rgb[2]}`
    if (seen.has(key)) return true
    seen.add(key)
  }
  return false
}

// A copy of the flattened buffer with the given components repainted to `rgb`. This is
// exactly what the server's reassign produces — both act on the same four-connected
// exact-palette regions — so the client can adopt it and skip re-decoding the raster.
export function pixelsWithComponents(source: Pixels, components: Component[], rgb: Rgb): Pixels {
  const { width, height } = source
  const data = new Uint8ClampedArray(source.data)
  for (const { bounds, mask } of components) {
    for (let ly = 0; ly < bounds.height; ly++) {
      for (let lx = 0; lx < bounds.width; lx++) {
        if (!mask[ly * bounds.width + lx]) continue
        const offset = ((bounds.y + ly) * width + (bounds.x + lx)) * 4
        data[offset] = rgb[0]; data[offset + 1] = rgb[1]; data[offset + 2] = rgb[2]
      }
    }
  }
  return { data, width, height }
}

// A copy of the flattened buffer with each staged pencil cell set to its label's colour,
// mirroring the server's paint_pixels so the raster need not be re-decoded on commit.
export function pixelsWithPaintedCells(source: Pixels, painted: Map<number, number>, palette: PaletteEntry[]): Pixels {
  const data = new Uint8ClampedArray(source.data)
  const rgbByLabel = new Map(palette.map(entry => [entry.index, entry.rgb]))
  for (const [cell, label] of painted) {
    const rgb = rgbByLabel.get(label)
    if (!rgb) continue
    const offset = cell * 4
    data[offset] = rgb[0]; data[offset + 1] = rgb[1]; data[offset + 2] = rgb[2]
  }
  return { data, width: source.width, height: source.height }
}

// RGBA overlay marking the selection, cropped to its bounding box: a translucent fill
// with an opaque outline so it reads over both light and dark artwork.
export function overlayPixels(selection: Component[]): OverlayRegion | null {
  const combined = combinedMask(selection)
  if (!combined) return null
  const { bounds, mask } = combined
  const { width: bw, height: bh } = bounds
  const data = new Uint8ClampedArray(bw * bh * 4)
  for (let ly = 0; ly < bh; ly++) {
    for (let lx = 0; lx < bw; lx++) {
      const cell = ly * bw + lx
      if (!mask[cell]) continue
      // The bounding box is tight, so a selected pixel on the box edge necessarily
      // borders unselected space — a boundary of the selection.
      const border = lx === 0 || ly === 0 || lx === bw - 1 || ly === bh - 1
        || !mask[cell - 1] || !mask[cell + 1] || !mask[cell - bw] || !mask[cell + bw]
      const offset = cell * 4
      if (border) {
        data[offset] = 255; data[offset + 1] = 255; data[offset + 2] = 255; data[offset + 3] = 235
      } else {
        data[offset] = 56; data[offset + 1] = 132; data[offset + 2] = 255; data[offset + 3] = 90
      }
    }
  }
  return { bounds, data }
}
