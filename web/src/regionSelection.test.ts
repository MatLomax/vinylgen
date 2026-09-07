import { describe, expect, it } from 'vitest'
import {
  combinedMask, componentIndexAt, floodComponent, hexToRgb, labelForRgb, neighbourLabels,
  overlayPixels, paintOverlayPixels, recolourPreviewPixels, regionReloadDecision, removeComponents, selectionLabels, toggleComponent, type Pixels, type Rgb,
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
  it('floods the four-connected component of one colour', () => {
    const component = floodComponent(makePixels(), 0, 0, palette)!
    expect(component.label).toBe(0)
    expect(component.point).toEqual([0, 0])
    expect([...component.mask].reduce((sum, v) => sum + v, 0)).toBe(4) // the 2x2 top-left A block
    expect(component.mask[0 * 4 + 0]).toBe(1) // width*y + x indexing via cell
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
    expect(componentIndexAt(selection, pixels, 1, 1)).toBe(0) // (1,1) is inside the A component
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

  it('marks selected pixels in the overlay and leaves the rest transparent', () => {
    const pixels = makePixels()
    const selection = toggleComponent([], pixels, 0, 0, palette)
    const overlay = overlayPixels(selection, pixels.width, pixels.height)
    expect(overlay[(0 * 4 + 0) * 4 + 3]).toBeGreaterThan(0) // (0,0) selected -> non-zero alpha
    expect(overlay[(0 * 4 + 2) * 4 + 3]).toBe(0) // (2,0) unselected -> transparent
    const size = pixels.width * pixels.height
    expect([...combinedMask(selection, size)].reduce((sum, v) => sum + v, 0)).toBe(4)
  })

  it('paints staged pencil cells in their target colour at full opacity', () => {
    // Stage cell (1,0) as colour C and cell (0,2) as colour B on a 4x3 grid.
    const painted = new Map<number, number>([[0 * 4 + 1, 2], [2 * 4 + 0, 1]])
    const overlay = paintOverlayPixels(painted, palette, 4, 3)
    const at = (x: number, y: number) => overlay.slice((y * 4 + x) * 4, (y * 4 + x) * 4 + 4)
    expect([...at(1, 0)]).toEqual([...C, 255]) // painted C, opaque
    expect([...at(0, 2)]).toEqual([...B, 255]) // painted B, opaque
    expect([...at(0, 0)]).toEqual([0, 0, 0, 0]) // untouched -> transparent
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
