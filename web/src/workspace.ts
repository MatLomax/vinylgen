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

export function midpoint(a: Point, b: Point): Point {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
}

export function distance(a: Point, b: Point) {
  return Math.hypot(b.x - a.x, b.y - a.y)
}

export function sameImageSize(a?: ImageSize, b?: ImageSize) {
  return Boolean(a && b && a.width === b.width && a.height === b.height)
}
