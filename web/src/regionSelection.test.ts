import { describe, expect, it } from 'vitest'
import {
  combinedMask, componentIndexAt, floodComponent, labelForRgb, neighbourLabels,
  overlayPixels, selectionLabels, toggleComponent, type Pixels, type Rgb,
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
})
