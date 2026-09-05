export type ViewTransform = { x: number; y: number; scale: number }
export type Point = { x: number; y: number }
export type ImageSize = { width: number; height: number }

export const MIN_SCALE = 0.05
export const MAX_SCALE = 32

export function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(maximum, Math.max(minimum, value))
}

export function zoomAt(transform: ViewTransform, point: Point, factor: number): ViewTransform {
  const scale = clamp(transform.scale * factor, MIN_SCALE, MAX_SCALE)
  const ratio = scale / transform.scale
  return {
    scale,
    x: point.x - (point.x - transform.x) * ratio,
    y: point.y - (point.y - transform.y) * ratio,
  }
}

export function fitTransform(viewWidth: number, viewHeight: number, imageWidth: number, imageHeight: number): ViewTransform {
  if (!viewWidth || !viewHeight || !imageWidth || !imageHeight) return { x: 0, y: 0, scale: 1 }
  const scale = clamp(Math.min((viewWidth - 32) / imageWidth, (viewHeight - 32) / imageHeight), MIN_SCALE, MAX_SCALE)
  return {
    scale,
    x: (viewWidth - imageWidth * scale) / 2,
    y: (viewHeight - imageHeight * scale) / 2,
  }
}

/** Convert a viewport-relative comparison divider to the image's local coordinates. */
export function splitPositionInImage(viewWidth: number, splitPercent: number, imageWidth: number, transform: ViewTransform) {
  if (!imageWidth || !transform.scale) return 0
  const dividerX = viewWidth * clamp(splitPercent, 0, 100) / 100
  return clamp((dividerX - transform.x) / transform.scale, 0, imageWidth)
}

/**
 * Choose distinct Before/After comparison images from the ordered image ids.
 * Before defaults to the preferred id when present, otherwise the first image.
 * After prefers the first distinct image whose intrinsic pixel dimensions are
 * known to match Before (via the optional dimensions map), so a default
 * comparison lands on a pair that can actually register; when no distinct image
 * is known-compatible yet — including at first render before any dimensions have
 * loaded — it falls back to the first distinct id. A two-image (or larger) set
 * therefore never resolves Before and After to the same artifact; a single image
 * yields an empty After (no counterpart to compare against).
 */
export function defaultComparePair(imageIds: string[], preferredId?: string, dimensions?: Record<string, ImageSize>): { beforeId: string; afterId: string } {
  const beforeId = preferredId && imageIds.includes(preferredId) ? preferredId : imageIds[0] ?? ''
  const distinct = imageIds.filter(id => id !== beforeId)
  const beforeSize = dimensions?.[beforeId]
  const compatible = beforeSize ? distinct.find(id => sameImageSize(beforeSize, dimensions?.[id])) : undefined
  const afterId = compatible ?? distinct[0] ?? ''
  return { beforeId, afterId }
}

export function midpoint(a: Point, b: Point): Point {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
}

export function distance(a: Point, b: Point) {
  return Math.hypot(b.x - a.x, b.y - a.y)
}

export function sameImageSize(a?: ImageSize, b?: ImageSize) {
  return Boolean(a && b && a.width === b.width && a.height === b.height)
}

/**
 * The integer pixels a pencil stroke covers moving to (toX, toY). With no prior
 * point it is just the destination; otherwise it is every step along the line
 * from the last painted pixel, so a fast drag leaves no gaps. The starting pixel
 * is omitted because the previous move already painted it.
 */
export function pixelLine(fromX: number | null, fromY: number | null, toX: number, toY: number): Array<[number, number]> {
  if (fromX === null || fromY === null) return [[toX, toY]]
  const steps = Math.max(Math.abs(toX - fromX), Math.abs(toY - fromY))
  if (steps === 0) return [[toX, toY]]
  const points: Array<[number, number]> = []
  for (let step = 1; step <= steps; step++) {
    points.push([Math.round(fromX + (toX - fromX) * step / steps), Math.round(fromY + (toY - fromY) * step / steps)])
  }
  return points
}
