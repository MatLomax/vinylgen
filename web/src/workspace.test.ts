import { describe, expect, it } from 'vitest'
import { defaultComparePair, fitTransform, gridsRegister, pixelLine, sameImageSize, splitPositionInImage, zoomAt } from './workspace'

describe('shared workspace transform', () => {
  it('keeps the pixel under the pointer registered while zooming', () => {
    const original = { x: 15, y: 25, scale: 2 }
    const pointer = { x: 215, y: 125 }
    const pixel = { x: (pointer.x - original.x) / original.scale, y: (pointer.y - original.y) / original.scale }
    const zoomed = zoomAt(original, pointer, 1.75)
    expect(zoomed.x + pixel.x * zoomed.scale).toBeCloseTo(pointer.x)
    expect(zoomed.y + pixel.y * zoomed.scale).toBeCloseTo(pointer.y)
  })

  it('fits landscape and portrait images inside phone viewports', () => {
    for (const [width, height] of [[1600, 900], [900, 1600]]) {
      const fitted = fitTransform(320, 480, width, height)
      expect(width * fitted.scale).toBeLessThanOrEqual(288.000001)
      expect(height * fitted.scale).toBeLessThanOrEqual(448.000001)
      expect(fitted.x).toBeGreaterThanOrEqual(15.999999)
      expect(fitted.y).toBeGreaterThanOrEqual(15.999999)
    }
  })

  it('clamps extreme zoom gestures', () => {
    expect(zoomAt({ x: 0, y: 0, scale: 1 }, { x: 0, y: 0 }, 100).scale).toBe(32)
    expect(zoomAt({ x: 0, y: 0, scale: 1 }, { x: 0, y: 0 }, .0001).scale).toBe(.05)
  })

  it('keeps the split mask registered with its viewport divider after pan and zoom', () => {
    const transform = { x: -300, y: 80, scale: 2 }
    const localSplit = splitPositionInImage(1000, 40, 1200, transform)

    expect(localSplit).toBe(350)
    expect(transform.x + localSplit * transform.scale).toBe(400)
  })

  it('clips the comparison fully when a panned image is entirely to one side of the divider', () => {
    expect(splitPositionInImage(1000, 50, 200, { x: 600, y: 0, scale: 1 })).toBe(0)
    expect(splitPositionInImage(1000, 50, 200, { x: 0, y: 0, scale: 1 })).toBe(200)
  })

  it('produces a new centered fit when the viewport changes orientation', () => {
    const portrait = fitTransform(320, 640, 1600, 900)
    const landscape = fitTransform(640, 320, 1600, 900)

    expect(portrait).not.toEqual(landscape)
    expect(1600 * landscape.scale).toBeLessThanOrEqual(608.000001)
    expect(900 * landscape.scale).toBeLessThanOrEqual(288.000001)
    expect(landscape.x).toBeCloseTo((640 - 1600 * landscape.scale) / 2)
    expect(landscape.y).toBeCloseTo((320 - 900 * landscape.scale) / 2)
  })

  it('only registers layers whose intrinsic pixel canvases match exactly', () => {
    expect(sameImageSize({ width: 1024, height: 768 }, { width: 1024, height: 768 })).toBe(true)
    expect(sameImageSize({ width: 1024, height: 768 }, { width: 1024, height: 900 })).toBe(false)
    expect(sameImageSize({ width: 1024, height: 768 }, undefined)).toBe(false)
  })

  it('registers grids that are equal or one integer upscale of the other', () => {
    expect(gridsRegister({ width: 40, height: 32 }, { width: 40, height: 32 })).toBe(true)
    expect(gridsRegister({ width: 40, height: 32 }, { width: 120, height: 96 })).toBe(true)
    expect(gridsRegister({ width: 120, height: 96 }, { width: 40, height: 32 })).toBe(true)
    // A different factor per axis, a fractional factor, or an unknown grid never registers.
    expect(gridsRegister({ width: 40, height: 32 }, { width: 80, height: 96 })).toBe(false)
    expect(gridsRegister({ width: 40, height: 32 }, { width: 60, height: 48 })).toBe(false)
    expect(gridsRegister({ width: 40, height: 32 }, undefined)).toBe(false)
  })

  it('chooses distinct before/after images and no counterpart for a lone image', () => {
    // Two or more images always resolve to different Before/After artifacts, so a
    // split can never clip an image over itself.
    expect(defaultComparePair(['source', 'flat', 'report'])).toEqual({ beforeId: 'source', afterId: 'flat' })
    expect(defaultComparePair(['source', 'flat'], 'flat')).toEqual({ beforeId: 'flat', afterId: 'source' })
    // A preferred id that is also the only remaining candidate still yields a distinct after.
    expect(defaultComparePair(['source', 'flat'], 'source').afterId).not.toBe('source')
    // A single image has no counterpart to compare against.
    expect(defaultComparePair(['only'])).toEqual({ beforeId: 'only', afterId: '' })
    expect(defaultComparePair([])).toEqual({ beforeId: '', afterId: '' })
  })

  it('prefers a dimension-compatible distinct after, skipping an earlier incompatible image', () => {
    const dimensions = {
      a: { width: 1000, height: 700 },
      b: { width: 1000, height: 850 },
      c: { width: 1000, height: 700 },
    }
    // b is earlier than c but incompatible with a, so After skips b for the compatible c.
    expect(defaultComparePair(['a', 'b', 'c'], undefined, dimensions)).toEqual({ beforeId: 'a', afterId: 'c' })
    // With no dimensions known yet it still resolves a distinct After (first distinct).
    expect(defaultComparePair(['a', 'b', 'c'])).toEqual({ beforeId: 'a', afterId: 'b' })
    expect(defaultComparePair(['a', 'b', 'c'], undefined, {})).toEqual({ beforeId: 'a', afterId: 'b' })
    // When no distinct image is known-compatible, it falls back to the first distinct id.
    expect(defaultComparePair(['a', 'b'], undefined, dimensions)).toEqual({ beforeId: 'a', afterId: 'b' })
  })

  it('paints only the destination pixel when a stroke has no prior point', () => {
    expect(pixelLine(null, null, 4, 7)).toEqual([[4, 7]])
    expect(pixelLine(4, 7, 4, 7)).toEqual([[4, 7]])
  })

  it('fills every gap-free pixel between the last painted pixel and the next', () => {
    // A fast diagonal drag must leave a continuous stroke, not scattered dots.
    expect(pixelLine(0, 0, 3, 3)).toEqual([[1, 1], [2, 2], [3, 3]])
    expect(pixelLine(0, 0, 4, 0)).toEqual([[1, 0], [2, 0], [3, 0], [4, 0]])
    // The starting pixel is omitted (the previous move already painted it).
    expect(pixelLine(0, 0, 3, 3)).not.toContainEqual([0, 0])
  })
})
