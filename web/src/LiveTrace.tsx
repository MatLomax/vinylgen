import { memo } from 'react'
import type { FaceEvent, PaletteColour } from './api'
import type { ImageSize } from './workspace'

// The live trace view: the Trace canvas draws each face of a running trace as t3 emits
// it. Faces arrive in t3's emission order and are kept, never held back; the canvas draws
// them in the finished edge SVG's paint order, redrawn on each arrival: one layer per
// palette colour in palette order (the omitted colour has none), each layer's faces in face
// order. The finished SVG orders a layer's faces by area instead; the faces of one layer
// share one fill and never overlap, so their order changes no pixel, and the composite on
// the canvas equals the finished edge SVG's.

// The faces received so far, by label, each label's in face order.
export type LiveFaces = ReadonlyMap<number, readonly FaceEvent[]>
export const NO_FACES: LiveFaces = new Map()

/** `faces` with `face` added in its place; a face already held is replaced. */
export function withFace(faces: LiveFaces, face: FaceEvent): LiveFaces {
  const layer = faces.get(face.label) ?? []
  let low = 0, high = layer.length
  while (low < high) {
    const middle = (low + high) >> 1
    if (layer[middle].face < face.face) low = middle + 1
    else high = middle
  }
  const replaces = layer[low]?.face === face.face
  const next = new Map(faces)
  next.set(face.label, [...layer.slice(0, low), face, ...layer.slice(low + (replaces ? 1 : 0))])
  return next
}

/** The layers to draw, bottom to top: each palette colour but `omitted`, in palette order, with its faces. */
export function paintOrder(faces: LiveFaces, palette: readonly PaletteColour[], omitted: number | null): { colour: PaletteColour; faces: readonly FaceEvent[] }[] {
  return [...palette].sort((a, b) => a.index - b.index)
    .filter(colour => colour.index !== omitted && (faces.get(colour.index)?.length ?? 0) > 0)
    .map(colour => ({ colour, faces: faces.get(colour.index)! }))
}

const FacePath = memo(function FacePath({ d }: { d: string }) { return <path d={d} /> })

/**
 * The faces of a running trace over the image stage: an SVG on the traced grid (`traced`,
 * the map's pixel units) stretched over the stage, so it tracks pan and zoom exactly.
 */
export function LiveTraceLayer({ faces, palette, omitted, traced }: { faces: LiveFaces; palette: readonly PaletteColour[]; omitted: number | null; traced: ImageSize }) {
  return <svg className="live-trace" viewBox={`0 0 ${traced.width} ${traced.height}`} preserveAspectRatio="none" role="img" aria-label="Live trace">
    {paintOrder(faces, palette, omitted).map(({ colour, faces: layer }) => <g key={colour.index} fill={colour.hex} fillRule="evenodd" data-label={colour.index}>
      {layer.map(face => <FacePath key={face.face} d={face.rings} />)}
    </g>)}
  </svg>
}
