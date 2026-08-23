import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'

afterEach(() => vi.unstubAllGlobals())

describe('abortable project artifact requests', () => {
  it('passes the caller signal to project detail requests', async () => {
    const controller = new AbortController()
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ revisions: [] })))
    vi.stubGlobal('fetch', fetch)

    await api.project('project-1', controller.signal)

    expect(fetch).toHaveBeenCalledWith('/api/projects/project-1', { signal: controller.signal })
  })

  it('passes the caller signal to revision artifact requests', async () => {
    const controller = new AbortController()
    const fetch = vi.fn().mockResolvedValue(new Response('[]'))
    vi.stubGlobal('fetch', fetch)

    await api.artifacts('revision-1', controller.signal)

    expect(fetch).toHaveBeenCalledWith('/api/revisions/revision-1/artifacts', { signal: controller.signal })
  })

  it('submits typed label operations to an immutable revision endpoint', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 'edited' })))
    vi.stubGlobal('fetch', fetch)

    await api.applyEdits('project-1', 'revision-1', [{ tool: 'pencil', label: 2, points: [[4, 5]] }])

    expect(fetch).toHaveBeenCalledWith('/api/projects/project-1/revisions/revision-1/edits', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ operations: [{ tool: 'pencil', label: 2, points: [[4, 5]] }] }),
    })
  })

  it('starts text-only source generation with complete settings', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ project: {}, job: {}, expanded_prompt: 'expanded' })))
    vi.stubGlobal('fetch', fetch)
    await api.generate('sky whale', 'Sky Whale', { width: 1536, height: 1024, preset: 'balanced', quality: 'medium', required_motifs: [], rejected_motifs: ['text'] })
    const request = JSON.parse(fetch.mock.calls[0][1].body)
    expect(request).toEqual({ prompt: 'sky whale', project_name: 'Sky Whale', provider: 'codex', settings: { width: 1536, height: 1024, preset: 'balanced', quality: 'medium', required_motifs: [], rejected_motifs: ['text'] } })
    expect(request).not.toHaveProperty('image')
    expect(request).not.toHaveProperty('revision_id')
  })

  it('submits a full-size mask and applies a selected candidate', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ revision: {}, candidates: [], expanded_prompt: 'expanded' })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 'applied' })))
    vi.stubGlobal('fetch', fetch)
    const mask = new Blob(['mask'], { type: 'image/png' })

    await api.maskedEdit('project-1', 'revision-1', mask, 'brass dome', 'replace', 3, 48)
    const form = fetch.mock.calls[0][1].body as FormData
    expect(form.get('prompt')).toBe('brass dome')
    expect(form.get('count')).toBe('3')
    expect(form.get('padding')).toBe('48')
    await api.applyMaskedEdit('project-1', 'candidates-1', 2)
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ candidate_revision_id: 'candidates-1', candidate_index: 2 })
  })
})
