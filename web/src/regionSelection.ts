export type Rgb = readonly [number, number, number]
export type Pixels = { data: Uint8ClampedArray; width: number; height: number }
export type PaletteEntry = { index: number; rgb: Rgb }
export type Component = { point: [number, number]; rgb: Rgb; label: number; mask: Uint8Array }

function colourAt(pixels: Pixels, x: number, y: number): Rgb {
  const offset = (y * pixels.width + x) * 4
  return [pixels.data[offset], pixels.data[offset + 1], pixels.data[offset + 2]]
}

function matches(pixels: Pixels, x: number, y: number, rgb: Rgb): boolean {
  const offset = (y * pixels.width + x) * 4
  return pixels.data[offset] === rgb[0] && pixels.data[offset + 1] === rgb[1] && pixels.data[offset + 2] === rgb[2]
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

// The four-connected component of identically-coloured pixels containing (x, y).
// Matching by exact RGB mirrors the authoritative label component the backend
// recomputes on commit, because a flattened raster holds only exact palette colours.
export function floodComponent(pixels: Pixels, x: number, y: number, palette: PaletteEntry[]): Component | null {
  const { width, height } = pixels
  if (x < 0 || y < 0 || x >= width || y >= height) return null
  const rgb = colourAt(pixels, x, y)
  const mask = new Uint8Array(width * height)
  const start = y * width + x
  mask[start] = 1
  const stack = [start]
  while (stack.length) {
    const cell = stack.pop()!
    const cx = cell % width
    const cy = (cell - cx) / width
    const neighbours: Array<[number, number]> = [[cx - 1, cy], [cx + 1, cy], [cx, cy - 1], [cx, cy + 1]]
    for (const [nx, ny] of neighbours) {
      if (nx < 0 || ny < 0 || nx >= width || ny >= height) continue
      const cellNeighbour = ny * width + nx
      if (!mask[cellNeighbour] && matches(pixels, nx, ny, rgb)) {
        mask[cellNeighbour] = 1
        stack.push(cellNeighbour)
      }
    }
  }
  return { point: [x, y], rgb, label: labelForRgb(rgb, palette), mask }
}

export function componentIndexAt(selection: Component[], pixels: Pixels, x: number, y: number): number {
  const cell = y * pixels.width + x
  return selection.findIndex(component => component.mask[cell] === 1)
}

// Add the component under (x, y) to the selection, or remove it if already selected.
export function toggleComponent(selection: Component[], pixels: Pixels, x: number, y: number, palette: PaletteEntry[]): Component[] {
  const existing = componentIndexAt(selection, pixels, x, y)
  if (existing >= 0) return selection.filter((_, index) => index !== existing)
  const component = floodComponent(pixels, x, y, palette)
  return component ? [...selection, component] : selection
}

export function combinedMask(selection: Component[], size: number): Uint8Array {
  const mask = new Uint8Array(size)
  for (const component of selection) {
    for (let cell = 0; cell < size; cell++) if (component.mask[cell]) mask[cell] = 1
  }
  return mask
}

export function selectionLabels(selection: Component[]): number[] {
  return selection.map(component => component.label)
}

// Palette indices that border the selection but are not themselves selected.
// Applying one of these is a "dissolve into a surrounding colour" rather than a
// free recolour.
export function neighbourLabels(selection: Component[], pixels: Pixels, palette: PaletteEntry[]): number[] {
  const { width, height } = pixels
  const combined = combinedMask(selection, width * height)
  const selected = new Set(selectionLabels(selection))
  const labels = new Set<number>()
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      if (!combined[y * width + x]) continue
      const neighbours: Array<[number, number]> = [[x - 1, y], [x + 1, y], [x, y - 1], [x, y + 1]]
      for (const [nx, ny] of neighbours) {
        if (nx < 0 || ny < 0 || nx >= width || ny >= height) continue
        if (combined[ny * width + nx]) continue
        const label = labelForRgb(colourAt(pixels, nx, ny), palette)
        if (label >= 0 && !selected.has(label)) labels.add(label)
      }
    }
  }
  return [...labels]
}

// RGBA overlay painting each staged pencil cell with its target palette colour at
// full opacity, so an uncommitted stroke previews exactly as it will flatten. Keyed
// by cell index (y * width + x) mapped to the palette label to paint there.
export function paintOverlayPixels(painted: Map<number, number>, palette: PaletteEntry[], width: number, height: number): Uint8ClampedArray {
  const overlay = new Uint8ClampedArray(width * height * 4)
  const rgbByLabel = new Map(palette.map(entry => [entry.index, entry.rgb]))
  for (const [cell, label] of painted) {
    const rgb = rgbByLabel.get(label)
    if (!rgb) continue
    const offset = cell * 4
    overlay[offset] = rgb[0]; overlay[offset + 1] = rgb[1]; overlay[offset + 2] = rgb[2]; overlay[offset + 3] = 255
  }
  return overlay
}

// RGBA overlay marking the selection: a translucent fill with an opaque outline
// so it reads over both light and dark artwork.
export function overlayPixels(selection: Component[], width: number, height: number): Uint8ClampedArray {
  const size = width * height
  const combined = combinedMask(selection, size)
  const overlay = new Uint8ClampedArray(size * 4)
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const cell = y * width + x
      if (!combined[cell]) continue
      const border = x === 0 || y === 0 || x === width - 1 || y === height - 1
        || !combined[cell - 1] || !combined[cell + 1] || !combined[cell - width] || !combined[cell + width]
      const offset = cell * 4
      if (border) {
        overlay[offset] = 255; overlay[offset + 1] = 255; overlay[offset + 2] = 255; overlay[offset + 3] = 235
      } else {
        overlay[offset] = 56; overlay[offset + 1] = 132; overlay[offset + 2] = 255; overlay[offset + 3] = 90
      }
    }
  }
  return overlay
}
