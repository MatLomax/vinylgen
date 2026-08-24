// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, type Artifact, type EditorState } from './api'
import { FlattenedEditor } from './FlattenedEditor'

const state: EditorState = {
  width: 1000, height: 500, protected_pixels: 0,
  palette: [
    { index: 0, name: 'black', hex: '#000000', rgb: [0, 0, 0], share_percent: 60 },
    { index: 1, name: 'white', hex: '#ffffff', rgb: [255, 255, 255], share_percent: 40 },
  ],
}
const flattened: Artifact = { id: 'flat-1', revision_id: 'rev-1', kind: 'flattened', filename: 'flattened.png', media_type: 'image/png', byte_size: 10, download_url: '/flattened.png' }

beforeEach(() => vi.spyOn(api, 'editor').mockResolvedValue(state))
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('palette-only flattened editing', () => {
  it('offers whole-colour merge and no manual painting or colour adjustment controls', async () => {
    render(<FlattenedEditor projectId="project-1" revisionId="rev-1" flattened={flattened} onCommit={() => undefined} />)
    await screen.findByRole('group', { name: 'Merge complete colours' })
    for (const removed of ['brush', 'pencil', 'lasso', 'flood fill', 'protect', 'Editable flattened label map']) {
      expect(screen.queryByLabelText(removed) ?? screen.queryByRole('button', { name: removed })).toBeNull()
    }
    expect(screen.queryByDisplayValue('#000000')).toBeNull()
    expect(screen.getByText('#000000')).toBeTruthy()
  })

  it('confirms direction and commits an immutable whole-colour merge', async () => {
    const apply = vi.spyOn(api, 'applyEdits').mockResolvedValue({ id: 'rev-2' } as never)
    const committed = vi.fn()
    render(<FlattenedEditor projectId="project-1" revisionId="rev-1" flattened={flattened} onCommit={committed} />)
    const merge = await screen.findByRole('button', { name: 'Merge black into white' })
    expect(screen.getByText(/Every/).textContent).toContain('A new revision will be created')
    fireEvent.click(merge)
    await vi.waitFor(() => expect(apply).toHaveBeenCalledWith('project-1', 'rev-1', [{ tool: 'merge_colour', source_label: 0, target_label: 1 }]))
    expect(committed).toHaveBeenCalledWith('rev-2')
  })

  it('shows the new revision palette names and does not rename an untouched field on switch', async () => {
    const revisedState: EditorState = {
      ...state,
      palette: [
        { index: 0, name: 'charcoal', hex: '#000000', rgb: [0, 0, 0], share_percent: 60 },
        { index: 1, name: 'ivory', hex: '#ffffff', rgb: [255, 255, 255], share_percent: 40 },
      ],
    }
    vi.spyOn(api, 'editor').mockImplementation(async revisionId => (revisionId === 'rev-2' ? revisedState : state))
    const apply = vi.spyOn(api, 'applyEdits')
    const { rerender } = render(<FlattenedEditor projectId="project-1" revisionId="rev-1" flattened={flattened} onCommit={() => undefined} />)
    await screen.findByDisplayValue('black')

    rerender(<FlattenedEditor projectId="project-1" revisionId="rev-2" flattened={flattened} onCommit={() => undefined} />)
    await screen.findByDisplayValue('charcoal')
    expect(screen.queryByDisplayValue('black')).toBeNull()

    fireEvent.blur(screen.getByDisplayValue('charcoal'))
    expect(apply).not.toHaveBeenCalled()
  })

  it('announces editor load and merge failures', async () => {
    vi.spyOn(api, 'applyEdits').mockRejectedValue(new Error('merge rejected'))
    render(<FlattenedEditor projectId="project-1" revisionId="rev-1" flattened={flattened} onCommit={() => undefined} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Merge black into white' }))
    expect((await screen.findByRole('alert')).textContent).toContain('merge rejected')
  })
})
