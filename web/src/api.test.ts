import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, followJob, type UpscaleSettings } from './api'
import { DEFAULT_CLEANUP_SETTINGS, DEFAULT_PALETTE_SETTINGS, DEFAULT_TRACE_SETTINGS } from './PipelineControls'

afterEach(() => vi.unstubAllGlobals())

describe('abortable project artifact requests', () => {
  it('passes the caller signal to project detail requests', async () => {
    const controller = new AbortController()
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ revisions: [] })))
    vi.stubGlobal('fetch', fetch)

    await api.project('project-1', controller.signal)

    expect(fetch).toHaveBeenCalledWith('/api/projects/project-1', { signal: controller.signal })
  })

  it('asks for the raster from before cleanup only when requested', async () => {
    const controller = new AbortController()
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify([]))))
    vi.stubGlobal('fetch', fetch)
    await api.effectiveArtifacts('revision-1', controller.signal)
    await api.effectiveArtifacts('revision-1', controller.signal, { beforeCleanup: true })
    expect(fetch).toHaveBeenNthCalledWith(1, '/api/revisions/revision-1/effective-artifacts', { signal: controller.signal })
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/revisions/revision-1/effective-artifacts?before_cleanup=true', { signal: controller.signal })
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

  it('deletes a project through the project endpoint', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetch)

    await api.deleteProject('project-1')

    expect(fetch).toHaveBeenCalledWith('/api/projects/project-1', { method: 'DELETE' })
  })

  it('surfaces the server message when a project delete is refused', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("Cancel the project's active job before deleting it", { status: 409 }))
    vi.stubGlobal('fetch', fetch)

    await expect(api.deleteProject('project-1')).rejects.toThrow("Cancel the project's active job before deleting it")
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

  it('sends complete typed palette, cleanup and trace settings to pipeline jobs', async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ id: 'job' }))))
    vi.stubGlobal('fetch', fetch)
    await api.startPipeline('palette', 'project-1', 'revision-1', { palette: DEFAULT_PALETTE_SETTINGS })
    await api.startPipeline('trace', 'project-1', 'revision-1', { trace: DEFAULT_TRACE_SETTINGS })
    await api.startPipeline('cleanup', 'project-1', 'revision-1', { cleanup: DEFAULT_CLEANUP_SETTINGS })
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      kind: 'palette', project_id: 'project-1', revision_id: 'revision-1', palette: DEFAULT_PALETTE_SETTINGS,
    })
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({
      kind: 'trace', project_id: 'project-1', revision_id: 'revision-1', trace: DEFAULT_TRACE_SETTINGS,
    })
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual({
      kind: 'cleanup', project_id: 'project-1', revision_id: 'revision-1', cleanup: DEFAULT_CLEANUP_SETTINGS,
    })
    expect(Object.keys(DEFAULT_PALETTE_SETTINGS)).toHaveLength(29)
    // The same field counts as the server's TraceJobSettings and CleanupJobSettings.
    expect(Object.keys(DEFAULT_TRACE_SETTINGS)).toHaveLength(13)
    expect(Object.keys(DEFAULT_CLEANUP_SETTINGS)).toHaveLength(8)
  })

})

describe('pre-flatten generative upscale', () => {
  const SETTINGS: UpscaleSettings = {
    upscaler: '4xNomos8kSCHAT-S.pth', upscale_factor: 2, checkpoint: 'blue_pencil-XL-v7.0.0.safetensors',
    denoise: 0.7, controlnet: true, controlnet_strength: 0.4, sample_size: 1024, tile_size: 512, seed: null,
  }

  it('fetches enumerated upscale options with an abort signal', async () => {
    const controller = new AbortController()
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      upscalers: [], checkpoints: [], controlnets: [], defaults: SETTINGS, sample_size_max: 1024,
    })))
    vi.stubGlobal('fetch', fetch)

    await api.upscaleOptions(controller.signal)

    expect(fetch).toHaveBeenCalledWith('/api/upscale/options', { signal: controller.signal })
  })

  it('starts an upscale job against the source revision with the complete typed settings', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ project: {}, job: { id: 'job-1' } })))
    vi.stubGlobal('fetch', fetch)

    await api.upscale('project-1', 'revision-1', SETTINGS)

    expect(fetch).toHaveBeenCalledWith('/api/projects/project-1/revisions/revision-1/upscale', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ settings: SETTINGS }),
    })
  })
})

describe('image import upload', () => {
  class FakeXHR {
    static instances: FakeXHR[] = []
    upload: { onprogress?: (event: { lengthComputable: boolean; loaded: number; total: number }) => void } = {}
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    status = 0
    responseText = ''
    statusText = ''
    method = ''
    url = ''
    body: FormData | null = null
    constructor() { FakeXHR.instances.push(this) }
    open(method: string, url: string) { this.method = method; this.url = url }
    send(body: FormData) { this.body = body }
  }
  beforeEach(() => { FakeXHR.instances = [] })

  it('streams transfer progress and resolves the imported project over XHR', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXHR)
    const fractions: number[] = []
    const promise = api.importProject('Art', new File(['x'], 'art.png', { type: 'image/png' }), fraction => fractions.push(fraction))
    const xhr = FakeXHR.instances[0]

    expect(xhr.method).toBe('POST')
    expect(xhr.url).toBe('/api/projects/import')
    expect(xhr.body).toBeInstanceOf(FormData)
    expect(xhr.body?.get('name')).toBe('Art')
    expect(xhr.body?.get('source')).toBeInstanceOf(File)

    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 10 })
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 10, total: 10 })
    expect(fractions).toEqual([0.5, 1])

    xhr.status = 201
    xhr.responseText = JSON.stringify({ id: 'project-1', revisions: [] })
    xhr.onload?.()
    await expect(promise).resolves.toMatchObject({ id: 'project-1' })
  })

  it('rejects with the server message when the import is refused', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXHR)
    const promise = api.importProject('Art', new File(['x'], 'art.png', { type: 'image/png' }))
    const xhr = FakeXHR.instances[0]
    xhr.status = 413
    xhr.responseText = 'Image is over the upload limit'
    xhr.onload?.()
    await expect(promise).rejects.toThrow('Image is over the upload limit')
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
