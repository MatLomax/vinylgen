// @vitest-environment jsdom
// The main canvas's Compare control per stage, through the real App and ImageWorkspace.
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Artifact, Project, Revision } from './api'
import { canvasCompareBefore } from './App'

const mocks = vi.hoisted(() => ({
  projects: vi.fn(), project: vi.fn(), effectiveArtifacts: vi.fn(), artifacts: vi.fn(), editor: vi.fn(),
  inkscapeHandoff: vi.fn(), health: vi.fn(), followJob: vi.fn(), applyEdits: vi.fn(),
}))
vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>()
  return { ...original, api: { ...original.api, ...mocks }, followJob: mocks.followJob }
})

const project: Project = { id: 'project-1', slug: 'sky-whale', name: 'Sky whale', created_at: '2026-01-01', updated_at: '2026-01-01' }
const base: Revision = { id: 'revision-1', project_id: project.id, parent_revision_id: null, kind: 'source', message: null, settings: {}, created_at: '2026-01-01' }
// The upscale's recorded grids: 1496 x 1051 at 2x, each side rounded to a multiple of 8.
const upscaled: Revision = { ...base, id: 'upscaled-1', parent_revision_id: 'revision-1', kind: 'upscaled-source', created_at: '2026-01-02', settings: { parent_source_size: [1496, 1051], source_size: [2992, 2104] } }
const flatten: Revision = { ...base, id: 'palette-1', parent_revision_id: 'revision-1', kind: 'palette', settings: { palette: {} }, created_at: '2026-01-02' }
const trace: Revision = { ...base, id: 'trace-1', parent_revision_id: 'palette-1', kind: 'trace', created_at: '2026-01-03', settings: { trace: { overlap: 0, solid_underlayers: false } } }

function artifact(kind: string, id: string, revisionId: string, mediaType = 'image/png'): Artifact {
  return { id, revision_id: revisionId, kind, filename: `${kind}.png`, media_type: mediaType, byte_size: 1, download_url: `/artifacts/${id}` }
}
const originalSource = artifact('source', 'source-id', 'revision-1')
const upscaledSource = artifact('source', 'upscaled-source-id', 'upscaled-1')
const traced = [
  originalSource, artifact('flattened', 'flattened-id', 'palette-1'),
  artifact('segmentation', 'segmentation-id', 'palette-1', 'application/octet-stream'),
  artifact('traced-edge', 'traced-edge-id', 'trace-1', 'image/svg+xml'), artifact('traced-overlap', 'traced-overlap-id', 'trace-1', 'image/svg+xml'),
]

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  window.scrollTo = vi.fn()
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  mocks.projects.mockResolvedValue([project])
  mocks.artifacts.mockResolvedValue([])
  mocks.editor.mockResolvedValue({ width: 40, height: 30, protected_pixels: 0, palette: [] })
  mocks.inkscapeHandoff.mockResolvedValue({ exports: [], shared_folder_enabled: false, guidance: '' })
  mocks.health.mockResolvedValue({ status: 'ok', max_upload_bytes: 100 * 1024 * 1024 })
  mocks.followJob.mockReturnValue(() => undefined)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

// Report every loaded canvas image at `sizes[artifact id]`.
function measure(sizes: Record<string, [number, number]>) {
  for (const image of document.querySelectorAll<HTMLImageElement>('.artwork-surface img')) {
    const id = image.src.split('/artifacts/')[1]
    const size = id && sizes[id]
    if (!size) continue
    Object.defineProperties(image, { naturalWidth: { configurable: true, value: size[0] }, naturalHeight: { configurable: true, value: size[1] } })
    fireEvent.load(image)
  }
}
const compareGroup = () => document.querySelector<HTMLElement>('.artwork-surface [role="group"][aria-label="Compare"]')

describe('canvas comparison per stage', () => {
  it('compares an upscaled source with the source it was made from on the Upload canvas', async () => {
    mocks.project.mockResolvedValue({ ...project, revisions: [upscaled, base] })
    mocks.effectiveArtifacts.mockImplementation(async (id: string) => id === 'upscaled-1' ? [upscaledSource] : [originalSource])
    window.history.replaceState(null, '', `/#stage=upload&project=${project.id}&revision=upscaled-1`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(mocks.effectiveArtifacts).toHaveBeenCalledWith('revision-1', expect.anything()))
    await vi.waitFor(() => expect(document.querySelector('img[src="/artifacts/source-id"]')).toBeTruthy())
    // The upscaler rounds each side to a multiple of 8: 1496 x 1051 at 2x is 2992 x 2104.
    measure({ 'upscaled-source-id': [2992, 2104], 'source-id': [1496, 1051] })
    await vi.waitFor(() => expect(compareGroup()).toBeTruthy())
    fireEvent.click(within(compareGroup()!).getByRole('button', { name: 'split' }))
    const layer = document.querySelector('.artwork-surface .compare-layer') as HTMLElement
    expect(within(layer).getByAltText('source (before upscale) · source.png')).toBeTruthy()
    expect(document.querySelector('.artwork-surface .image-stage > img')?.getAttribute('src')).toBe('/artifacts/upscaled-source-id')
  })

  it('shows the source on the Upload canvas, whatever view another stage left, and compares it', async () => {
    const flattenedUpscale: Revision = { ...flatten, parent_revision_id: 'upscaled-1', created_at: '2026-01-03' }
    mocks.project.mockResolvedValue({ ...project, revisions: [flattenedUpscale, upscaled, base] })
    mocks.effectiveArtifacts.mockImplementation(async (id: string) => id === 'palette-1'
      ? [upscaledSource, artifact('flattened', 'flattened-id', 'palette-1'), artifact('segmentation', 'segmentation-id', 'palette-1', 'application/octet-stream')]
      : [originalSource])
    window.history.replaceState(null, '', `/#stage=flatten&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    const views = await screen.findAllByRole('group', { name: 'Artwork view' })
    fireEvent.click(within(views[0]).getByRole('button', { name: 'Flattened' }))
    await vi.waitFor(() => expect(document.querySelector('.artwork-surface .image-stage > img')?.getAttribute('src')).toBe('/artifacts/flattened-id'))
    // Flatten does not use the pre-upscale source, so it is not loaded there.
    expect(mocks.effectiveArtifacts).not.toHaveBeenCalledWith('revision-1', expect.anything())
    fireEvent.click(screen.getByRole('tab', { name: 'Upload' }))
    await vi.waitFor(() => expect(document.querySelector('.artwork-surface .image-stage > img')?.getAttribute('src')).toBe('/artifacts/upscaled-source-id'))
    await vi.waitFor(() => expect(document.querySelector('img[src="/artifacts/source-id"]')).toBeTruthy())
    measure({ 'upscaled-source-id': [2992, 2104], 'source-id': [1496, 1051], 'flattened-id': [2992, 2104] })
    await vi.waitFor(() => expect(compareGroup()).toBeTruthy())
    // No view toggle on Upload: it always shows the source.
    expect(document.querySelector('header [role="group"][aria-label="Artwork view"]')).toBeNull()
  })

  it('reports a failed pre-upscale source load in the Compare control, and Retry loads it', async () => {
    mocks.project.mockResolvedValue({ ...project, revisions: [upscaled, base] })
    let fail = true
    mocks.effectiveArtifacts.mockImplementation(async (id: string) => {
      if (id === 'upscaled-1') return [upscaledSource]
      if (fail) throw new Error('offline')
      return [originalSource]
    })
    window.history.replaceState(null, '', `/#stage=upload&project=${project.id}&revision=upscaled-1`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(compareGroup()).toBeTruthy())
    expect(within(compareGroup()!).getByRole('alert').textContent).toContain('Could not load the source from before the upscale: Error: offline')
    // Reported locally, not in the global banner.
    expect(document.querySelector('.global-error')).toBeNull()
    fail = false
    fireEvent.click(within(compareGroup()!).getByRole('button', { name: 'Retry' }))
    await vi.waitFor(() => expect(document.querySelector('img[src="/artifacts/source-id"]')).toBeTruthy())
    measure({ 'upscaled-source-id': [2992, 2104], 'source-id': [1496, 1051] })
    await vi.waitFor(() => expect(within(compareGroup()!).getByRole('button', { name: 'split' })).toBeTruthy())
    expect(within(compareGroup()!).queryByRole('alert')).toBeNull()
  })

  it('has no Compare control on the Upload canvas of a source that was not upscaled', async () => {
    mocks.project.mockResolvedValue({ ...project, revisions: [base] })
    mocks.effectiveArtifacts.mockResolvedValue([originalSource])
    window.history.replaceState(null, '', `/#stage=upload&project=${project.id}&revision=revision-1`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(document.querySelector('.artwork-surface .image-stage > img')).toBeTruthy())
    measure({ 'source-id': [1000, 700] })
    expect(compareGroup()).toBeNull()
    expect(mocks.effectiveArtifacts).toHaveBeenCalledTimes(1)
  })

  it('compares the traced SVG with the raster it traced on the Trace canvas', async () => {
    mocks.project.mockResolvedValue({ ...project, revisions: [trace, flatten, base] })
    mocks.effectiveArtifacts.mockResolvedValue(traced)
    window.history.replaceState(null, '', `/#stage=trace&project=${project.id}&revision=trace-1`)
    render(createElement((await import('./App')).default))
    const views = await screen.findAllByRole('group', { name: 'Artwork view' })
    fireEvent.click(within(views[0]).getByRole('button', { name: 'Traced' }))
    await vi.waitFor(() => expect(document.querySelector('.artwork-surface .image-stage > img')?.getAttribute('src')).toBe('/artifacts/traced-edge-id'))
    // The SVG reports a CSS-pixel size; it adopts the flattened raster's grid.
    measure({ 'source-id': [1000, 700], 'flattened-id': [1000, 700], 'traced-edge-id': [300, 210], 'traced-overlap-id': [300, 210] })
    await vi.waitFor(() => expect(compareGroup()).toBeTruthy())
    fireEvent.click(within(compareGroup()!).getByRole('button', { name: 'blink' }))
    fireEvent.click(within(compareGroup()!).getByRole('button', { name: 'Show before' }))
    expect(document.querySelector('.artwork-surface .compare-layer img')?.getAttribute('src')).toBe('/artifacts/flattened-id')
    // On the Source view the Trace canvas shows its input's input, not its result: no comparison.
    fireEvent.click(within(views[0]).getByRole('button', { name: 'Source' }))
    await vi.waitFor(() => expect(compareGroup()).toBeNull())
  })
})

describe('canvasCompareBefore', () => {
  const images = { originalSource: 'original', source: 'source', preEdit: 'pre-edit', beforeCleanup: 'pre-cleanup', raster: 'raster', all: ['source', 'raster', 'traced', 'pre-edit'] }

  it('names each stage its natural input', () => {
    expect(canvasCompareBefore('upload', 'source', false, images)).toEqual(['original'])
    // Upload always shows the source, whatever view is chosen elsewhere.
    expect(canvasCompareBefore('upload', 'cleaned', false, images)).toEqual(['original'])
    expect(canvasCompareBefore('flatten', 'flattened', false, images)).toEqual(['source'])
    expect(canvasCompareBefore('cleanup', 'cleaned', false, images)).toEqual(['pre-cleanup'])
    expect(canvasCompareBefore('trace', 'traced', false, images)).toEqual(['raster'])
  })

  it('offers Palette and Edit every image, the raster before the edits first, else the source', () => {
    expect(canvasCompareBefore('edit', 'edited', false, images)).toEqual(['pre-edit', 'source', 'raster', 'traced'])
    expect(canvasCompareBefore('palette', 'flattened', false, { ...images, preEdit: undefined, all: ['raster', 'source'] })).toEqual(['source', 'raster'])
  })

  it('puts the view before the edited one first on Palette and Edit (editReference)', () => {
    // Editing the Edited image: the Cleaned one first; editing the Cleaned image: the
    // Flattened one; the pre-edit original and the rest follow, each listed once.
    expect(canvasCompareBefore('edit', 'edited', false, { ...images, editReference: 'cleaned', all: ['source', 'cleaned', 'pre-edit'] }))
      .toEqual(['cleaned', 'pre-edit', 'source'])
    expect(canvasCompareBefore('palette', 'cleaned', false, { ...images, editReference: 'pre-cleanup', all: ['pre-cleanup', 'source'] }))
      .toEqual(['pre-cleanup', 'pre-edit', 'source'])
    // Still nothing while a point is picked.
    expect(canvasCompareBefore('edit', 'edited', true, { ...images, editReference: 'cleaned' })).toEqual([])
  })

  it('offers nothing while the canvas shows something other than its stage result, or a point is picked', () => {
    expect(canvasCompareBefore('upload', 'source', false, { ...images, originalSource: undefined })).toEqual([])
    expect(canvasCompareBefore('flatten', 'source', false, images)).toEqual([])
    expect(canvasCompareBefore('cleanup', 'edited', false, images)).toEqual([])
    expect(canvasCompareBefore('cleanup', 'cleaned', false, { ...images, beforeCleanup: undefined })).toEqual([])
    expect(canvasCompareBefore('trace', 'cleaned', false, images)).toEqual([])
    expect(canvasCompareBefore('flatten', 'flattened', true, images)).toEqual([])
    expect(canvasCompareBefore('export', 'traced', false, images)).toEqual([])
  })
})

describe('Palette and Edit while comparing', () => {
  const colour = (index: number, name: string, hex: string, rgb: [number, number, number]) => ({ index, name, hex, rgb, share_percent: 50 })
  const flattenedRows = [originalSource, artifact('flattened', 'flattened-id', 'palette-1'), artifact('segmentation', 'segmentation-id', 'palette-1', 'application/octet-stream')]
  // The 2 x 1 flattened raster decodes to navy|orange.
  class DecodedImage {
    onload: (() => void) | null = null
    naturalWidth = 2
    naturalHeight = 1
    set src(_value: string) { queueMicrotask(() => this.onload?.()) }
  }

  async function open(stage: 'edit' | 'palette') {
    vi.stubGlobal('Image', DecodedImage)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      drawImage: () => undefined, getImageData: () => ({ data: new Uint8ClampedArray([15, 35, 60, 255, 225, 95, 45, 255]) }),
      createImageData: (width: number, height: number) => ({ data: new Uint8ClampedArray(width * height * 4) }),
      putImageData: () => undefined, setTransform: () => undefined, clearRect: () => undefined,
    } as unknown as CanvasRenderingContext2D)
    mocks.project.mockResolvedValue({ ...project, revisions: [flatten, base] })
    mocks.effectiveArtifacts.mockResolvedValue(flattenedRows)
    mocks.editor.mockResolvedValue({ width: 2, height: 1, protected_pixels: 0, palette: [colour(0, 'Navy', '#0F233C', [15, 35, 60]), colour(1, 'Orange', '#E15F2D', [225, 95, 45])] })
    mocks.applyEdits.mockResolvedValue({ ...flatten, id: 'edit-1', parent_revision_id: 'palette-1', kind: 'label-edit' })
    window.history.replaceState(null, '', `/#stage=${stage}&project=${project.id}&revision=palette-1`)
    render(createElement((await import('./App')).default))
    await vi.waitFor(() => expect(document.querySelector('img[src="/artifacts/flattened-id"]')).toBeTruthy())
    const viewport = document.querySelector<HTMLElement>('.artwork-surface .viewport')!
    Object.defineProperties(viewport, { clientWidth: { configurable: true, value: 800 }, clientHeight: { configurable: true, value: 600 } })
    viewport.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}) })
    HTMLElement.prototype.setPointerCapture = () => undefined
    measure({ 'source-id': [2, 1], 'flattened-id': [2, 1] })
    await vi.waitFor(() => expect(compareGroup()).toBeTruthy())
    return viewport
  }
  afterEach(() => { vi.restoreAllMocks() })
  const panel = (name: string) => screen.getByRole('tabpanel', { name })
  const describedBy = (control: HTMLElement) => document.getElementById(control.getAttribute('aria-describedby') ?? '')?.textContent

  it('commits no area or pencil edit, and says why, until Compare is off', async () => {
    localStorage.setItem('vinylgen.canvasCompare', 'split')
    const viewport = await open('edit')
    const swatch = () => panel('Edit').querySelector<HTMLButtonElement>('button[aria-label*="Orange"]')!
    // The centre of image pixel (x, y) on screen, from the canvas's own transform.
    const pixelAt = (x: number, y: number) => {
      const [, left, top, scale] = document.querySelector<HTMLElement>('.artwork-surface .image-stage')!.style.transform.match(/translate3d\(([-\d.]+)px, ([-\d.]+)px, 0\) scale\(([-\d.]+)\)/)!.map(Number)
      return { clientX: left + (x + .5) * scale, clientY: top + (y + .5) * scale }
    }
    const selectNavy = async () => {
      await vi.waitFor(() => expect(within(panel('Edit')).getByText('No areas selected yet.')).toBeTruthy())
      fireEvent.pointerDown(viewport, { pointerId: 1, ...pixelAt(0, 0) })
      fireEvent.pointerUp(viewport, { pointerId: 1, ...pixelAt(0, 0) })
      expect(swatch().disabled).toBe(false)
    }
    await selectNavy()
    // Edit opens with Compare Off even with Split remembered from another canvas.
    expect(within(compareGroup()!).getByRole('button', { name: 'Off' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(within(compareGroup()!).getByRole('button', { name: 'split' }))
    // Locked, not disabled: still focusable, activation does nothing, the reason attached.
    expect(swatch().disabled).toBe(false)
    expect(swatch().getAttribute('aria-disabled')).toBe('true')
    swatch().focus()
    expect(document.activeElement).toBe(swatch())
    expect(describedBy(swatch())).toBe('Turn Compare off to edit')
    const undo = within(panel('Edit')).getByRole('button', { name: 'Undo' })
    expect(undo.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(swatch())
    fireEvent.click(undo)
    expect(mocks.applyEdits).not.toHaveBeenCalled()
    expect(window.location.hash).toContain('revision=palette-1')
    expect(within(compareGroup()!).getByText('Turn Compare off to edit')).toBeTruthy()
    fireEvent.click(within(panel('Edit')).getByRole('button', { name: 'Pencil' }))
    const apply = within(panel('Edit')).getByRole('button', { name: 'Apply pencil edits' }) as HTMLButtonElement
    expect(describedBy(apply)).toBe('Turn Compare off to edit')
    // Painting on the canvas is off too, so nothing is staged.
    fireEvent.pointerDown(viewport, { pointerId: 2, ...pixelAt(1, 0) })
    fireEvent.pointerUp(viewport, { pointerId: 2, ...pixelAt(1, 0) })
    expect(within(panel('Edit')).getByText('Turn Compare off to edit')).toBeTruthy()
    fireEvent.click(apply)
    expect(mocks.applyEdits).not.toHaveBeenCalled()
    fireEvent.click(within(panel('Edit')).getByRole('button', { name: 'Select areas' }))
    fireEvent.click(within(compareGroup()!).getByRole('button', { name: 'Off' }))
    await selectNavy()
    fireEvent.click(swatch())
    await vi.waitFor(() => expect(mocks.applyEdits).toHaveBeenCalledTimes(1))
  })

  it('opens Edit with Compare Off after Split was chosen on Palette, with editing working', async () => {
    const viewport = await open('palette')
    const recolour = () => within(panel('Palette')).getByRole('button', { name: 'Recolour Navy' }) as HTMLButtonElement
    await vi.waitFor(() => expect(recolour().disabled).toBe(false))
    fireEvent.click(within(compareGroup()!).getByRole('button', { name: 'split' }))
    expect(recolour().getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(screen.getByRole('tab', { name: 'Edit' }))
    await vi.waitFor(() => expect(within(compareGroup()!).getByRole('button', { name: 'Off' }).getAttribute('aria-pressed')).toBe('true'))
    expect(document.querySelector('.artwork-surface .compare-layer')).toBeNull()
    const swatch = () => panel('Edit').querySelector<HTMLButtonElement>('button[aria-label*="Orange"]')!
    await vi.waitFor(() => expect(within(panel('Edit')).getByText('No areas selected yet.')).toBeTruthy())
    const [, left, top, scale] = document.querySelector<HTMLElement>('.artwork-surface .image-stage')!.style.transform.match(/translate3d\(([-\d.]+)px, ([-\d.]+)px, 0\) scale\(([-\d.]+)\)/)!.map(Number)
    const at = { clientX: left + .5 * scale, clientY: top + .5 * scale }
    fireEvent.pointerDown(viewport, { pointerId: 1, ...at })
    fireEvent.pointerUp(viewport, { pointerId: 1, ...at })
    expect(swatch().disabled).toBe(false)
    expect(swatch().getAttribute('aria-disabled')).toBeNull()
    fireEvent.click(swatch())
    await vi.waitFor(() => expect(mocks.applyEdits).toHaveBeenCalledTimes(1))
  })

  it('opens Palette with Compare Off and locks its side panel while comparing', async () => {
    localStorage.setItem('vinylgen.canvasCompare', 'blink')
    await open('palette')
    const recolour = () => within(panel('Palette')).getByRole('button', { name: 'Recolour Navy' }) as HTMLButtonElement
    await vi.waitFor(() => expect(recolour().disabled).toBe(false))
    expect(within(compareGroup()!).getByRole('button', { name: 'Off' }).getAttribute('aria-pressed')).toBe('true')
    expect(recolour().getAttribute('aria-disabled')).toBeNull()
    fireEvent.click(within(compareGroup()!).getByRole('button', { name: 'blink' }))
    expect(recolour().getAttribute('aria-disabled')).toBe('true')
    expect(describedBy(recolour())).toBe('Turn Compare off to edit')
    // The reason is a status region, announced when it appears.
    expect(document.getElementById(recolour().getAttribute('aria-describedby')!)!.getAttribute('role')).toBe('status')
    expect(within(compareGroup()!).getByText('Turn Compare off to edit')).toBeTruthy()
    fireEvent.click(recolour())
    expect(recolour().getAttribute('aria-expanded')).toBe('false')
    const merge = within(panel('Palette')).getByRole('button', { name: /^Merge / }) as HTMLButtonElement
    expect(merge.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(merge)
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(mocks.applyEdits).not.toHaveBeenCalled()
    fireEvent.click(within(compareGroup()!).getByRole('button', { name: 'Off' }))
    expect(recolour().getAttribute('aria-disabled')).toBeNull()
    // Palette keeps the other canvases' remembered mode as it was.
    expect(localStorage.getItem('vinylgen.canvasCompare')).toBe('blink')
  })
})
