import { describe, expect, it } from 'vitest'
import type { Revision } from './api'
import { DEFAULT_PALETTE_SETTINGS } from './PipelineControls'
import { mapPalettePointsToGrid, mapPointToGrid, palettePointsForRevision, sourceGridSteps } from './sourceGrid'

const base: Revision = { id: 'root', project_id: 'p', parent_revision_id: null, kind: 'source', message: null, settings: {}, created_at: '2026-01-01' }
const upscaled = (id: string, parent: string, from: [number, number], to: [number, number]): Revision =>
  ({ ...base, id, parent_revision_id: parent, kind: 'upscaled-source', settings: { parent_source_size: from, source_size: to, stale_concerns: ['palette'] } })
const child = (id: string, parent: string, kind = 'palette'): Revision => ({ ...base, id, parent_revision_id: parent, kind })

describe('mapPointToGrid', () => {
  it('maps a pixel to the pixel holding its centre on the new grid', () => {
    // An exact 2x grid: pixel x covers [2x, 2x+2); its centre 2x+1 is in pixel 2x+1.
    expect(mapPointToGrid([0, 0], [64, 48], [128, 96])).toEqual([1, 1])
    expect(mapPointToGrid([10, 20], [64, 48], [128, 96])).toEqual([21, 41])
    // The same grid is the identity.
    expect(mapPointToGrid([7, 5], [64, 48], [64, 48])).toEqual([7, 5])
  })

  it('handles a non-integer ratio, such as a factor rounded to a multiple of 8', () => {
    // 64x48 at 2.1x rounded to 136x104: x' = floor((x + .5) * 136 / 64), y' = floor((y + .5) * 104 / 48).
    expect(mapPointToGrid([0, 0], [64, 48], [136, 104])).toEqual([1, 1])
    expect(mapPointToGrid([31, 23], [64, 48], [136, 104])).toEqual([66, 50])
    expect(mapPointToGrid([63, 47], [64, 48], [136, 104])).toEqual([134, 102])
    // Each axis scales on its own ratio.
    expect(mapPointToGrid([3, 3], [10, 20], [15, 20])).toEqual([5, 3])
  })

  it('keeps the edges on the grid, growing or shrinking', () => {
    expect(mapPointToGrid([63, 47], [64, 48], [128, 96])).toEqual([127, 95])
    expect(mapPointToGrid([0, 0], [1023, 767], [1016, 760])).toEqual([0, 0])
    expect(mapPointToGrid([1022, 766], [1023, 767], [1016, 760])).toEqual([1015, 759])
    // A point outside the old grid (never a valid pick) is clamped onto the new one.
    expect(mapPointToGrid([500, 500], [64, 48], [128, 96])).toEqual([127, 95])
    // Shrinking folds neighbours into one pixel: 3 -> floor(3.5 * 2 / 8) = 0, 4 -> floor(4.5 / 4) = 1.
    expect(mapPointToGrid([3, 4], [8, 8], [2, 2])).toEqual([0, 1])
  })

  it('round-trips a grow then shrink onto the same pixel', () => {
    for (const x of [0, 1, 17, 40, 63]) {
      const up = mapPointToGrid([x, x % 48], [64, 48], [136, 104])
      expect(mapPointToGrid(up, [136, 104], [64, 48])).toEqual([x, x % 48])
    }
  })
})

describe('mapPalettePointsToGrid', () => {
  it('maps every point kind, both ends of a pair, and leaves the other fields alone', () => {
    const palette = { ...DEFAULT_PALETTE_SETTINGS, initial_colours: 9,
      protected_component_points: [[10, 20]] as [number, number][], preserved_colour_points: [[0, 47]] as [number, number][],
      distinct_colour_point_pairs: [[[63, 0], [5, 6]]] as [[number, number], [number, number]][] }
    expect(mapPalettePointsToGrid(palette, [64, 48], [128, 96])).toEqual({ ...palette,
      protected_component_points: [[21, 41]], preserved_colour_points: [[1, 95]], distinct_colour_point_pairs: [[[127, 1], [11, 13]]] })
    // A partial record gains no point fields it did not have.
    expect(mapPalettePointsToGrid({ initial_colours: 9 }, [64, 48], [128, 96])).toEqual({ initial_colours: 9 })
  })
})

describe('sourceGridSteps', () => {
  const revisions = [
    base, child('flat', 'root'), upscaled('up1', 'flat', [64, 48], [136, 104]), child('flat2', 'up1'),
    upscaled('up2', 'flat2', [136, 104], [272, 208]), child('flat3', 'up2'), child('sibling', 'root'),
  ]

  it('lists each upscale crossed down from an ancestor, oldest first', () => {
    expect(sourceGridSteps(revisions, 'flat', 'flat3')).toEqual([
      { from: [64, 48], to: [136, 104] }, { from: [136, 104], to: [272, 208] }])
    expect(sourceGridSteps(revisions, 'up1', 'flat3')).toEqual([{ from: [136, 104], to: [272, 208] }])
    expect(sourceGridSteps(revisions, 'flat3', 'up2')).toEqual([])
  })

  it('undoes an upscale on the way up to a common ancestor', () => {
    expect(sourceGridSteps(revisions, 'flat2', 'sibling')).toEqual([{ from: [136, 104], to: [64, 48] }])
    expect(sourceGridSteps(revisions, 'flat', 'sibling')).toEqual([])
  })

  it('is unknown across unrelated roots or through an upscale without recorded grids', () => {
    const other = { ...base, id: 'other-root' }
    const legacy = { ...base, id: 'legacy', parent_revision_id: 'root', kind: 'upscaled-source', settings: {} }
    expect(sourceGridSteps([...revisions, other], 'flat', 'other-root')).toBeNull()
    expect(sourceGridSteps([...revisions, legacy], 'flat', 'legacy')).toBeNull()
    expect(sourceGridSteps(revisions, 'missing', 'flat')).toBeNull()
  })
})

describe('palettePointsForRevision', () => {
  it('maps through every upscale between the picking revision and the shown one', () => {
    const revisions = [base, upscaled('up1', 'root', [64, 48], [136, 104]), upscaled('up2', 'up1', [136, 104], [272, 208])]
    const palette = { protected_component_points: [[31, 23]] as [number, number][] }
    const once = mapPointToGrid([31, 23], [64, 48], [136, 104])
    expect(palettePointsForRevision(palette, revisions, 'root', 'up2').protected_component_points)
      .toEqual([mapPointToGrid(once, [136, 104], [272, 208])])
  })

  it('leaves the points as they are when the grid change is unknown', () => {
    const palette = { protected_component_points: [[31, 23]] as [number, number][] }
    expect(palettePointsForRevision(palette, [base], 'root', 'elsewhere')).toBe(palette)
    expect(palettePointsForRevision(palette, [base], 'root', 'root')).toBe(palette)
  })
})
