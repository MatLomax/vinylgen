import type { PaletteSettings, Revision } from './api'

// Source-pixel settings (the Flatten form's preservation points) follow the source they
// were picked on. An `upscaled-source` revision records the pixel grid of its parent's
// source and of its own; every other revision shares its parent's grid.

export type GridSize = [number, number]
type Point = [number, number]
type GridStep = { from: GridSize; to: GridSize }
type PalettePoints = Pick<PaletteSettings, 'protected_component_points' | 'preserved_colour_points' | 'distinct_colour_point_pairs'>

// The pixel of the `to` grid containing the centre of pixel `point` of the `from` grid,
// by relative position: x' = floor((x + 0.5) * toWidth / fromWidth), clamped to the grid.
export function mapPointToGrid(point: Point, from: GridSize, to: GridSize): Point {
  const axis = (value: number, before: number, after: number) =>
    Math.min(after - 1, Math.max(0, Math.floor((value + 0.5) * after / before)))
  return [axis(point[0], from[0], to[0]), axis(point[1], from[1], to[1])]
}

// `palette` with every preservation point (both ends of each separate pair) mapped from
// the `from` grid onto the `to` grid; other fields are untouched.
export function mapPalettePointsToGrid<T extends Partial<PaletteSettings>>(palette: T, from: GridSize, to: GridSize): T {
  const point = (value: Point) => mapPointToGrid(value, from, to)
  const mapped: Partial<PalettePoints> = {}
  if (palette.protected_component_points) mapped.protected_component_points = palette.protected_component_points.map(point)
  if (palette.preserved_colour_points) mapped.preserved_colour_points = palette.preserved_colour_points.map(point)
  if (palette.distinct_colour_point_pairs) mapped.distinct_colour_point_pairs = palette.distinct_colour_point_pairs.map(([first, second]): [Point, Point] => [point(first), point(second)])
  return { ...palette, ...mapped }
}

function gridSize(value: unknown): GridSize | null {
  return Array.isArray(value) && value.length === 2 && value.every(side => Number.isInteger(side) && side > 0) ? [value[0], value[1]] : null
}

// The grid changes between the source of revision `fromId` and that of `toId`, in the
// order a point crosses them: up from `fromId` to their nearest common ancestor (each
// upscale undone), then down to `toId` (each upscale applied). Empty when both share a
// grid; null when the change is unknown: the two share no ancestor, or an upscale on the
// way did not record its grids.
export function sourceGridSteps(revisions: Revision[], fromId: string, toId: string): GridStep[] | null {
  const byId = new Map(revisions.map(row => [row.id, row]))
  const lineage = (id: string): Revision[] => {
    const chain: Revision[] = []
    const seen = new Set<string>()
    for (let node = byId.get(id); node && !seen.has(node.id); node = node.parent_revision_id ? byId.get(node.parent_revision_id) : undefined) {
      seen.add(node.id)
      chain.push(node)
    }
    return chain
  }
  const up = lineage(fromId)
  const down = lineage(toId)
  const common = down.findIndex(node => up.some(other => other.id === node.id))
  if (common < 0) return null
  const commonId = down[common].id
  const steps: GridStep[] = []
  const change = (node: Revision, reverse: boolean): boolean => {
    if (node.kind !== 'upscaled-source') return true
    const before = gridSize(node.settings.parent_source_size)
    const after = gridSize(node.settings.source_size)
    if (!before || !after) return false
    steps.push(reverse ? { from: after, to: before } : { from: before, to: after })
    return true
  }
  for (const node of up.slice(0, up.findIndex(other => other.id === commonId))) if (!change(node, true)) return null
  for (const node of down.slice(0, common).reverse()) if (!change(node, false)) return null
  return steps
}

// `palette`, picked on the source of revision `fromId`, with its points mapped onto the
// source of revision `toId`. Left as it is when the grid change is unknown.
export function palettePointsForRevision<T extends Partial<PaletteSettings>>(palette: T, revisions: Revision[], fromId: string, toId: string): T {
  const steps = fromId === toId ? [] : sourceGridSteps(revisions, fromId, toId)
  return (steps ?? []).reduce((value, step) => mapPalettePointsToGrid(value, step.from, step.to), palette)
}
