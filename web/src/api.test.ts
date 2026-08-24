import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, followJob } from './api'
import { DEFAULT_PALETTE_SETTINGS, DEFAULT_TRACE_SETTINGS } from './PipelineControls'

afterEach(() => vi.unstubAllGlobals())

describe('abortable project artifact requests', () => {
  it('passes the caller signal to project detail requests', async () => {
    const controller = new AbortController()
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ revisions: [] })))
    vi.stubGlobal('fetch', fetch)

    await api.project('project-1', controller.signal)

    expect(fetch).toHaveBeenCalledWith('/api/projects/project-1', { signal: controller.signal })
  })

  it('passes the caller signal to Inkscape handoff requests', async () => {
    const controller = new AbortController()
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ exports: [], shared_folder_enabled: false, guidance: '' })))
    vi.stubGlobal('fetch', fetch)

    await api.inkscapeHandoff('revision-1', controller.signal)

    expect(fetch).toHaveBeenCalledWith('/api/revisions/revision-1/inkscape-handoff', { signal: controller.signal })
  })

  it('passes the caller signal to revision artifact requests', async () => {
    const controller = new AbortController()
    const fetch = vi.fn().mockResolvedValue(new Response('[]'))
    vi.stubGlobal('fetch', fetch)

    await api.artifacts('revision-1', controller.signal)

    expect(fetch).toHaveBeenCalledWith('/api/revisions/revision-1/artifacts', { signal: controller.signal })
  })

  it('submits typed whole-colour operations to an immutable revision endpoint', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 'edited' })))
    vi.stubGlobal('fetch', fetch)

    await api.applyEdits('project-1', 'revision-1', [{ tool: 'merge_colour', source_label: 2, target_label: 1 }])

    expect(fetch).toHaveBeenCalledWith('/api/projects/project-1/revisions/revision-1/edits', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ operations: [{ tool: 'merge_colour', source_label: 2, target_label: 1 }] }),
    })
  })

  it('starts text-only source generation with complete settings', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ project: {}, job: {}, expanded_prompt: 'expanded' })))
    vi.stubGlobal('fetch', fetch)
    await api.generate('sky whale', 'Sky Whale', { width: 1536, height: 1024, desired_colours: 7, required_motifs: [], rejected_motifs: ['text'] })
    const request = JSON.parse(fetch.mock.calls[0][1].body)
    expect(request).toEqual({ prompt: 'sky whale', project_name: 'Sky Whale', provider: 'codex', settings: { width: 1536, height: 1024, desired_colours: 7, required_motifs: [], rejected_motifs: ['text'] } })
    expect(request).not.toHaveProperty('image')
    expect(request).not.toHaveProperty('revision_id')
  })

  it('sends complete typed palette and trace settings to pipeline jobs', async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ id: 'job' }))))
    vi.stubGlobal('fetch', fetch)
    await api.startPipeline('palette', 'project-1', 'revision-1', { palette: DEFAULT_PALETTE_SETTINGS })
    await api.startPipeline('trace', 'project-1', 'revision-1', { trace: DEFAULT_TRACE_SETTINGS })
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      kind: 'palette', project_id: 'project-1', revision_id: 'revision-1', palette: DEFAULT_PALETTE_SETTINGS,
    })
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({
      kind: 'trace', project_id: 'project-1', revision_id: 'revision-1', trace: DEFAULT_TRACE_SETTINGS,
    })
    expect(Object.keys(DEFAULT_PALETTE_SETTINGS)).toHaveLength(21)
    expect(Object.keys(DEFAULT_TRACE_SETTINGS)).toHaveLength(21)
  })

})

describe('resilient job event subscription', () => {
  it('reports only a truly closed stream, ignoring transient reconnect errors', () => {
    const instances: FakeEventSource[] = []
    class FakeEventSource {
      static readonly CONNECTING = 0
      static readonly CLOSED = 2
      onerror: ((event: Event) => void) | null = null
      readyState = FakeEventSource.CONNECTING
      addEventListener = vi.fn()
      close = vi.fn()
      constructor(public url: string) { instances.push(this) }
    }
    vi.stubGlobal('EventSource', FakeEventSource)
    const onError = vi.fn()

    followJob('job-1', () => undefined, onError)
    const source = instances[0]

    source.readyState = FakeEventSource.CONNECTING
    source.onerror?.(new Event('error'))
    expect(onError).not.toHaveBeenCalled()

    source.readyState = FakeEventSource.CLOSED
    source.onerror?.(new Event('error'))
    expect(onError).toHaveBeenCalledTimes(1)
  })
})
