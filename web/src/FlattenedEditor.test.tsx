// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, type Artifact, type EditorState, type Revision } from './api'
import { FlattenedEditor } from './FlattenedEditor'
import './styles.css'

const state: EditorState = {
  width: 1000,
  height: 500,
  protected_pixels: 0,
  palette: [
    { index: 0, name: 'black', hex: '#000000', rgb: [0, 0, 0], share_percent: 60 },
    { index: 1, name: 'white', hex: '#ffffff', rgb: [255, 255, 255], share_percent: 40 },
  ],
}

const flattened: Artifact = {
  id: 'flat-1', revision_id: 'rev-1', kind: 'flattened', filename: 'flattened.png',
  media_type: 'image/png', byte_size: 10, download_url: '/flattened.png',
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('FlattenedEditor interactions', () => {
  it('maps a phone-scaled pointer gesture into exact image coordinates', async () => {
    vi.spyOn(api, 'editor').mockResolvedValue(state)
    const next = { id: 'rev-2' } as Revision
    const apply = vi.spyOn(api, 'applyEdits').mockResolvedValue(next)
    const committed = vi.fn()
    HTMLElement.prototype.setPointerCapture = () => undefined

    render(<FlattenedEditor projectId="project-1" revisionId="rev-1" flattened={flattened} onCommit={committed} />)
    const canvas = await screen.findByLabelText('Editable flattened label map') as HTMLCanvasElement
    canvas.width = 1000
    canvas.height = 500
    canvas.getBoundingClientRect = () => ({
      x: 10, y: 20, left: 10, top: 20, right: 210, bottom: 120,
      width: 200, height: 100, toJSON: () => ({}),
    })

    fireEvent.pointerDown(canvas, { pointerId: 7, clientX: 30, clientY: 30 })
    fireEvent.pointerMove(canvas, { pointerId: 7, clientX: 110, clientY: 70 })
    fireEvent.pointerUp(canvas, { pointerId: 7, clientX: 110, clientY: 70 })

    await waitFor(() => expect(apply).toHaveBeenCalledTimes(1))
    expect(apply).toHaveBeenCalledWith('project-1', 'rev-1', [{
      tool: 'brush', label: 0, radius: 6, points: [[100, 50], [500, 250]],
    }])
    await waitFor(() => expect(committed).toHaveBeenCalledWith('rev-2'))
  })

  it('exposes touch-sized editing actions at the component boundary', async () => {
    vi.spyOn(api, 'editor').mockResolvedValue(state)
    render(<FlattenedEditor projectId="project-1" revisionId="rev-1" flattened={flattened} onCommit={() => undefined} />)
    const brush = await screen.findByRole('button', { name: 'brush' })
    const swatch = screen.getByRole('button', { name: 'Select black' })
    expect(getComputedStyle(brush).minHeight).toBe('44px')
    expect(getComputedStyle(swatch).minHeight).toBe('44px')
    expect((screen.getByLabelText('Brush radius · 6px') as HTMLInputElement).type).toBe('range')
  })

  it('supports precise keyboard cursor movement and brush edits', async () => {
    vi.spyOn(api, 'editor').mockResolvedValue(state)
    const apply = vi.spyOn(api, 'applyEdits').mockResolvedValue({ id: 'rev-2' } as Revision)
    render(<FlattenedEditor projectId="project-1" revisionId="rev-1" flattened={flattened} onCommit={() => undefined} />)
    const canvas = await screen.findByLabelText('Editable flattened label map')

    expect(canvas.getAttribute('tabindex')).toBe('0')
    expect(screen.getByText(/Keyboard: arrows move the cursor/)).toBeTruthy()
    expect(screen.getByRole('status').textContent).toContain('x 500, y 250')
    fireEvent.keyDown(canvas, { key: 'ArrowRight', shiftKey: true })
    fireEvent.keyDown(canvas, { key: 'ArrowUp' })
    expect(screen.getByRole('status').textContent).toContain('x 510, y 249')
    fireEvent.keyDown(canvas, { key: ' ' })

    await waitFor(() => expect(apply).toHaveBeenCalledWith('project-1', 'rev-1', [{ tool: 'brush', label: 0, radius: 6, points: [[510, 249]] }]))
  })

  it('lets keyboard users build and commit a lasso', async () => {
    vi.spyOn(api, 'editor').mockResolvedValue(state)
    const apply = vi.spyOn(api, 'applyEdits').mockResolvedValue({ id: 'rev-2' } as Revision)
    render(<FlattenedEditor projectId="project-1" revisionId="rev-1" flattened={flattened} onCommit={() => undefined} />)
    const canvas = await screen.findByLabelText('Editable flattened label map')
    fireEvent.click(screen.getByRole('button', { name: 'lasso' }))
    fireEvent.keyDown(canvas, { key: ' ' })
    fireEvent.keyDown(canvas, { key: 'ArrowRight' })
    fireEvent.keyDown(canvas, { key: ' ' })
    fireEvent.keyDown(canvas, { key: 'ArrowDown' })
    fireEvent.keyDown(canvas, { key: ' ' })
    fireEvent.keyDown(canvas, { key: 'Enter' })
    await waitFor(() => expect(apply).toHaveBeenCalledWith('project-1', 'rev-1', [{ tool: 'lasso', label: 0, radius: 6, points: [[500, 250], [501, 250], [501, 251]] }]))
  })

  it('announces editor load and edit failures as alerts', async () => {
    vi.spyOn(api, 'editor').mockRejectedValueOnce(new Error('editor offline'))
    const view = render(<FlattenedEditor projectId="project-1" revisionId="rev-1" flattened={flattened} onCommit={() => undefined} />)
    expect((await screen.findByRole('alert')).textContent).toContain('editor offline')
    view.unmount()

    vi.spyOn(api, 'editor').mockResolvedValue(state)
    vi.spyOn(api, 'applyEdits').mockRejectedValue(new Error('edit rejected'))
    render(<FlattenedEditor projectId="project-1" revisionId="rev-1" flattened={flattened} onCommit={() => undefined} />)
    const canvas = await screen.findByLabelText('Editable flattened label map')
    fireEvent.keyDown(canvas, { key: ' ' })
    expect((await screen.findByRole('alert')).textContent).toContain('edit rejected')
  })
})
