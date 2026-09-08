import { describe, expect, it } from 'vitest'
import {
  combinedMask, componentIndexAt, floodComponent, hexToRgb, labelForRgb, neighbourLabels,
  overlayPixels, paintOverlayPixels, paletteHasDuplicateRgb, pixelsWithComponents, pixelsWithPaintedCells,
  recolourPreviewPixels, regionReloadDecision, removeComponents, selectionLabels, toggleComponent, type Pixels, type Rgb,
} from './regionSelection'

const A: Rgb = [10, 0, 0]
const B: Rgb = [0, 20, 0]
const C: Rgb = [0, 0, 30]
const palette = [{ index: 0, rgb: A }, { index: 1, rgb: B }, { index: 2, rgb: C }]

// A 4x3 flattened raster:
//   A A B B
//   A A B B
//   C C C C
function makePixels(): Pixels {
  const width = 4, height = 3
  const rows: Rgb[][] = [
    [A, A, B, B],
    [A, A, B, B],
    [C, C, C, C],
  ]
  const data = new Uint8ClampedArray(width * height * 4)
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const offset = (y * width + x) * 4
      const [r, g, b] = rows[y][x]
      data[offset] = r; data[offset + 1] = g; data[offset + 2] = b; data[offset + 3] = 255
    }
  }
  return { data, width, height }
}

describe('regionSelection', () => {
  it('floods the four-connected component of one colour, cropped to its bounds', () => {
    const component = floodComponent(makePixels(), 0, 0, palette)!
    expect(component.label).toBe(0)
    expect(component.point).toEqual([0, 0])
    // The A block is the top-left 2x2, so its mask is a 2x2 fully-set patch at (0,0).
    expect(component.bounds).toEqual({ x: 0, y: 0, width: 2, height: 2 })
    expect([...component.mask]).toEqual([1, 1, 1, 1])
  })

  it('maps a colour to its palette label, or -1 when unknown', () => {
    expect(labelForRgb(B, palette)).toBe(1)
    expect(labelForRgb([1, 2, 3], palette)).toBe(-1)
  })

  it('toggles a component in and back out of the selection', () => {
    const pixels = makePixels()
    let selection = toggleComponent([], pixels, 0, 0, palette) // add A
    selection = toggleComponent(selection, pixels, 2, 0, palette) // add B
    expect(selection).toHaveLength(2)
    expect(selectionLabels(selection).sort()).toEqual([0, 1])
    expect(componentIndexAt(selection, 1, 1)).toBe(0) // (1,1) is inside the A component
    selection = toggleComponent(selection, pixels, 1, 1, palette) // click A again -> remove A
    expect(selection).toHaveLength(1)
    expect(selectionLabels(selection)).toEqual([1])
  })

  it('removes the submitted batch by identity, keeping areas re-clicked after kickoff', () => {
    const pixels = makePixels()
    // Batch submitted to a replace: the A block and the B block.
    let selection = toggleComponent([], pixels, 0, 0, palette) // add A
    selection = toggleComponent(selection, pixels, 2, 0, palette) // add B
    const submitted = selection
    // After kickoff the user re-clicks the same A block; a distinct component object.
    const afterKickoff = toggleComponent(selection, pixels, 1, 1, palette) // A already present -> removes it...
    const withNewA = toggleComponent(afterKickoff, pixels, 0, 0, palette) // ...re-add A as a fresh component
    const remaining = removeComponents(withNewA, submitted)
    // The originally-submitted A and B are gone; the freshly-clicked A survives.
    expect(remaining).toHaveLength(1)
    expect(selectionLabels(remaining)).toEqual([0])
    expect(submitted.every(component => !remaining.includes(component))).toBe(true)
  })

  it('restores the surviving selection across the revision reload without clobbering later clicks', () => {
    const pixels = makePixels()
    const survivors = toggleComponent([], pixels, 0, 0, palette) // the post-kickoff selection
    const handoff = { id: 'rev-2', selection: survivors, applied: false }

    // Run 1: the revision id changes to the edit's revision -> restore the survivors.
    const first = regionReloadDecision(handoff, 'rev-2')
    expect(first.action).toBe('apply')
    if (first.action !== 'apply') throw new Error('unreachable')
    expect(first.selection).toBe(survivors)
    expect(first.handoff.applied).toBe(true)

    // Run 2: the reloaded flattened image settles (same revision) -> leave selection be,
    // so areas clicked between the two runs are not wiped.
    expect(regionReloadDecision(first.handoff, 'rev-2').action).toBe('keep')

    // Any other revision (undo, project switch, no handoff) clears the selection.
    expect(regionReloadDecision(first.handoff, 'rev-9').action).toBe('clear')
    expect(regionReloadDecision(null, 'rev-2').action).toBe('clear')
  })

  it('reports only bordering, non-selected colours as neighbours', () => {
    const pixels = makePixels()
    const a = toggleComponent([], pixels, 0, 0, palette)
    expect(neighbourLabels(a, pixels, palette).sort()).toEqual([1, 2]) // A touches B and C
    const ab = toggleComponent(a, pixels, 2, 0, palette)
    expect(neighbourLabels(ab, pixels, palette)).toEqual([2]) // A+B selected -> only C remains a neighbour
  })

  it('marks the selection overlay within its bounding box', () => {
    const pixels = makePixels()
    const selection = toggleComponent([], pixels, 0, 0, palette)
    const overlay = overlayPixels(selection)!
    // The A block is the top-left 2x2, so the overlay patch is that box; every pixel
    // in a 2x2 selection is a boundary pixel, hence the opaque outline alpha.
    expect(overlay.bounds).toEqual({ x: 0, y: 0, width: 2, height: 2 })
    for (let cell = 0; cell < 4; cell++) expect(overlay.data[cell * 4 + 3]).toBe(235)
    const combined = combinedMask(selection)!
    expect(combined.bounds).toEqual({ x: 0, y: 0, width: 2, height: 2 })
    expect([...combined.mask].reduce((sum, v) => sum + v, 0)).toBe(4)
  })

  it('paints staged pencil cells in their target colour, cropped to their bounds', () => {
    // Stage cell (1,0) as colour C and cell (0,2) as colour B on a 4x3 grid.
    const painted = new Map<number, number>([[0 * 4 + 1, 2], [2 * 4 + 0, 1]])
    const overlay = paintOverlayPixels(painted, palette, 4)!
    // The two cells span x in [0,1], y in [0,2].
    expect(overlay.bounds).toEqual({ x: 0, y: 0, width: 2, height: 3 })
    const at = (lx: number, ly: number) => [...overlay.data.slice((ly * 2 + lx) * 4, (ly * 2 + lx) * 4 + 4)]
    expect(at(1, 0)).toEqual([...C, 255]) // painted C, opaque
    expect(at(0, 2)).toEqual([...B, 255]) // painted B, opaque
    expect(at(0, 0)).toEqual([0, 0, 0, 0]) // untouched -> transparent
  })

  it('repaints selected components onto a raster copy, leaving the source and rest untouched', () => {
    const pixels = makePixels()
    const selection = toggleComponent([], pixels, 0, 0, palette) // the A block (top-left 2x2)
    const patched = pixelsWithComponents(pixels, selection, [7, 8, 9])
    const at = (data: Uint8ClampedArray, x: number, y: number) => [...data.slice((y * 4 + x) * 4, (y * 4 + x) * 4 + 3)]
    expect(at(patched.data, 0, 0)).toEqual([7, 8, 9]) // A -> new colour
    expect(at(patched.data, 1, 1)).toEqual([7, 8, 9]) // whole A block moves
    expect(at(patched.data, 2, 0)).toEqual([...B]) // B untouched
    expect(at(patched.data, 0, 2)).toEqual([...C]) // C untouched
    expect(at(pixels.data, 0, 0)).toEqual([...A]) // source buffer is not mutated
  })

  it('repaints staged pencil cells onto a raster copy at their label colours', () => {
    const pixels = makePixels()
    const painted = new Map<number, number>([[0 * 4 + 0, 1], [2 * 4 + 3, 0]]) // (0,0)->B, (3,2)->A
    const patched = pixelsWithPaintedCells(pixels, painted, palette)
    const at = (x: number, y: number) => [...patched.data.slice((y * 4 + x) * 4, (y * 4 + x) * 4 + 3)]
    expect(at(0, 0)).toEqual([...B])
    expect(at(3, 2)).toEqual([...A])
    expect(at(1, 1)).toEqual([...A]) // an unpainted A pixel is unchanged
    expect([...pixels.data.slice(0, 3)]).toEqual([...A]) // source untouched
  })

  it('flags a palette where two colours share an identical RGB', () => {
    // A distinct palette is safe for optimistic raster adoption; a duplicate RGB is not,
    // because an RGB flood can straddle two labels the server reassigns separately.
    expect(paletteHasDuplicateRgb(palette)).toBe(false)
    expect(paletteHasDuplicateRgb([{ index: 0, rgb: A }, { index: 1, rgb: B }, { index: 2, rgb: A }])).toBe(true)
  })

  it('parses full and shorthand hex into an rgb triple', () => {
    expect(hexToRgb('#336699')).toEqual([51, 102, 153])
    expect(hexToRgb('336699')).toEqual([51, 102, 153])
    expect(hexToRgb('#0f0')).toEqual([0, 255, 0])
  })

  it('recolours every pixel of one palette colour, leaving the rest exact', () => {
    const preview = recolourPreviewPixels(makePixels(), B, [7, 8, 9])
    const at = (x: number, y: number) => [...preview.slice((y * 4 + x) * 4, (y * 4 + x) * 4 + 4)]
    expect(at(2, 0)).toEqual([7, 8, 9, 255]) // was B -> new colour, opaque
    expect(at(3, 1)).toEqual([7, 8, 9, 255]) // the whole B block moves
    expect(at(0, 0)).toEqual([...A, 255]) // A untouched
    expect(at(0, 2)).toEqual([...C, 255]) // C untouched
  })
})
