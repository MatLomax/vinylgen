// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, type Artifact, type MaskedEditCandidates } from './api'
import { MaskedEditor } from './MaskedEditor'

const source: Artifact = {
  id: 'source-1', revision_id: 'rev-1', kind: 'source', filename: 'source.png',
  media_type: 'image/png', byte_size: 10, download_url: '/source.png',
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('MaskedEditor keyboard interactions', () => {
  it('moves a bounded keyboard cursor and paints a brush-sized dab', () => {
    const context = {
      clearRect: vi.fn(), beginPath: vi.fn(), arc: vi.fn(), fill: vi.fn(),
      globalCompositeOperation: '', fillStyle: '',
    }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D)
    render(<MaskedEditor projectId="project-1" revisionId="rev-1" source={source} onApply={() => undefined} />)
    const canvas = screen.getByLabelText('Masked edit painting canvas')

    expect(canvas.getAttribute('tabindex')).toBe('0')
    expect(screen.getByText(/Keyboard: arrows move the mask cursor/)).toBeTruthy()
    expect(screen.getByText(/Keyboard cursor: x 0, y 0 · paint mode/)).toBeTruthy()
    fireEvent.keyDown(canvas, { key: 'ArrowRight', shiftKey: true })
    fireEvent.keyDown(canvas, { key: 'ArrowDown' })
    expect(screen.getByText(/Keyboard cursor: x 2, y 1 · paint mode/)).toBeTruthy()
    fireEvent.keyDown(canvas, { key: ' ' })

    expect(context.beginPath).toHaveBeenCalledOnce()
    expect(context.arc).toHaveBeenCalledWith(2, 1, 18, 0, Math.PI * 2)
    expect(context.fill).toHaveBeenCalledOnce()
  })

  it('announces masked generation progress and completion once', async () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['mask'], { type: 'image/png' })))
    const result = {
      revision: { id: 'candidate-rev' }, expanded_prompt: 'A brass dome',
      candidates: [{ source, flattened_preview: source }],
    } as MaskedEditCandidates
    vi.spyOn(api, 'maskedEdit').mockResolvedValue(result)
    render(<MaskedEditor projectId="project-1" revisionId="rev-1" source={source} onApply={() => undefined} />)
    fireEvent.change(screen.getByPlaceholderText('e.g. a brass observation dome'), { target: { value: 'a brass dome' } })
    fireEvent.click(screen.getByRole('button', { name: 'Generate replacement' }))

    expect(screen.getByText('Generating masked edit candidates…').getAttribute('aria-atomic')).toBe('true')
    await waitFor(() => expect(screen.getByText('1 candidate ready.')).toBeTruthy())
    expect(screen.getAllByText('1 candidate ready.')).toHaveLength(1)
  })

  it('announces masked generation failures as alerts', async () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['mask'], { type: 'image/png' })))
    vi.spyOn(api, 'maskedEdit').mockRejectedValue(new Error('generation unavailable'))
    render(<MaskedEditor projectId="project-1" revisionId="rev-1" source={source} onApply={() => undefined} />)
    fireEvent.change(screen.getByPlaceholderText('e.g. a brass observation dome'), { target: { value: 'remove tower' } })
    fireEvent.click(screen.getByRole('button', { name: 'Generate replacement' }))
    expect((await screen.findByRole('alert')).textContent).toContain('generation unavailable')
  })

  it('announces candidate application failures as alerts', async () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['mask'], { type: 'image/png' })))
    vi.spyOn(api, 'maskedEdit').mockResolvedValue({
      revision: { id: 'candidate-rev' }, expanded_prompt: 'A brass dome',
      candidates: [{ source, flattened_preview: source }],
    } as MaskedEditCandidates)
    vi.spyOn(api, 'applyMaskedEdit').mockRejectedValue(new Error('apply rejected'))
    render(<MaskedEditor projectId="project-1" revisionId="rev-1" source={source} onApply={() => undefined} />)
    fireEvent.change(screen.getByPlaceholderText('e.g. a brass observation dome'), { target: { value: 'a brass dome' } })
    fireEvent.click(screen.getByRole('button', { name: 'Generate replacement' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Apply candidate 1' }))
    expect((await screen.findByRole('alert')).textContent).toContain('apply rejected')
  })
})
