export type Project = { id: string; slug: string; name: string; created_at: string; updated_at: string }
export type Revision = { id: string; project_id: string; parent_revision_id: string | null; kind: string; message: string | null; settings: Record<string, unknown>; created_at: string }
export type Artifact = { id: string; revision_id: string; kind: string; filename: string; media_type: string; byte_size: number; download_url: string }
export type Job = { id: string; project_id: string | null; revision_id: string | null; result_revision_id: string | null; kind: string; status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'; progress: number; message: string | null; error: string | null }
export type PaletteColour = { index: number; name: string; hex: string; rgb: [number, number, number]; share_percent: number }
export type EditorState = { width: number; height: number; palette: PaletteColour[]; protected_pixels: number }
export type EditOperation = { tool: 'merge_colour' | 'rename_colour' | 'recolour_colour' | 'reassign_region' | 'paint_pixels'; label?: number; source_label?: number; target_label?: number; name?: string; rgb?: [number, number, number]; point?: [number, number]; pixels?: [number, number][] }
export type PaletteSettings = {
  initial_colours: number; final_colours: number | null; histogram_bits: number; analysis_max_dimension: number
  similar_delta_e: number; small_share: number; small_delta_e: number
  fragmented_share: number; fragmented_largest_region: number; fragmented_delta_e: number
  min_output_colours: number; min_region_pixels: number; min_colour_share: number
  intentional_line_min_contrast: number; intentional_line_min_length: number
  aa_ambiguity_margin: number; aa_smoothness: number; aa_resolution_passes: number; flatten_upscale: number; flatten_smooth_edges: boolean
  protected_component_points: [number, number][]; preserved_colour_points: [number, number][]
  distinct_colour_point_pairs: [[number, number], [number, number]][]
}
export type TraceSettings = {
  width_mm: number | null; height_mm: number | null; overlap: number; solid_underlayers: boolean
  remove_colour: string | null; layer_order: string[] | null
  corner_threshold: number; curve_optimization: boolean; curve_fit_tolerance: number
  straight_line_max_deviation: number; minimum_straight_length: number
  max_deviation: number | null; corner_analysis_span: number
  cleanup: boolean; cleanup_action: 'remove' | 'widen' | 'remove_then_widen'
  min_detail_width: number; min_region_area: number; remove_below_width: number
  soft_path_nodes: number; soft_layer_nodes: number; soft_document_nodes: number
}
export type GenerationSettings = { width: number; height: number; desired_colours: number; required_motifs: string[]; rejected_motifs: string[] }
export type GenerationStarted = { project: Project; job: Job; expanded_prompt: string }
export type InkscapeHandoff = { exports: Artifact[]; shared_folder_enabled: boolean; guidance: string }

async function result<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error((await response.text()) || response.statusText)
  return response.json() as Promise<T>
}

export const api = {
  projects: () => fetch('/api/projects').then(result<Project[]>),
  project: (projectId: string, signal?: AbortSignal) => fetch(`/api/projects/${projectId}`, { signal }).then(result<Project & { revisions: Revision[] }>),
  artifacts: (revisionId: string, signal?: AbortSignal) => fetch(`/api/revisions/${revisionId}/artifacts`, { signal }).then(result<Artifact[]>),
  effectiveArtifacts: (revisionId: string, signal?: AbortSignal) => fetch(`/api/revisions/${revisionId}/effective-artifacts`, { signal }).then(result<Artifact[]>),
  editor: (revisionId: string, signal?: AbortSignal) => fetch(`/api/revisions/${revisionId}/editor`, { signal }).then(result<EditorState>),
  applyEdits: (projectId: string, revisionId: string, operations: EditOperation[]) => fetch(`/api/projects/${projectId}/revisions/${revisionId}/edits`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ operations }),
  }).then(result<Revision>),
  importProject: (name: string, file: File) => {
    const body = new FormData()
    body.append('name', name)
    body.append('source', file)
    return fetch('/api/projects/import', { method: 'POST', body }).then(result<Project & { revisions: Revision[] }>)
  },
  generate: (prompt: string, projectName: string, settings: GenerationSettings) => fetch('/api/generations', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt, project_name: projectName || null, provider: 'codex', settings }),
  }).then(result<GenerationStarted>),
  inkscapeHandoff: (revisionId: string, signal?: AbortSignal) => fetch(`/api/revisions/${revisionId}/inkscape-handoff`, { signal }).then(result<InkscapeHandoff>),
  saveForInkscape: (revisionId: string, artifactId: string) => {
    const body = new FormData(); body.append('artifact_id', artifactId)
    return fetch(`/api/revisions/${revisionId}/inkscape-save`, { method: 'POST', body }).then(result<{ filename: string; shared_path: string }>)
  },
  reimportInkscape: (projectId: string, revisionId: string, file: File) => {
    const body = new FormData(); body.append('svg', file)
    return fetch(`/api/projects/${projectId}/revisions/${revisionId}/inkscape-reimport`, { method: 'POST', body }).then(result<Revision>)
  },
  startDemo: (projectId: string) => fetch('/api/jobs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind: 'foundation-demo', project_id: projectId }),
  }).then(result<Job>),
  startPipeline: (kind: 'palette' | 'trace' | 'validate' | 'export', projectId: string, revisionId: string,
    settings?: { palette?: PaletteSettings; trace?: TraceSettings; colour_names?: Record<string, string> }) => fetch('/api/jobs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, project_id: projectId, revision_id: revisionId, ...settings }),
  }).then(result<Job>),
  cancelJob: (jobId: string) => fetch(`/api/jobs/${jobId}/cancel`, { method: 'POST' }).then(result<Job>),
}

export function followJob(jobId: string, onJob: (job: Job) => void, onError: (error: Event) => void) {
  const events = new EventSource(`/api/jobs/${jobId}/events`)
  events.addEventListener('job', event => {
    const job = JSON.parse((event as MessageEvent).data) as Job
    onJob(job)
    if (['succeeded', 'failed', 'cancelled'].includes(job.status)) events.close()
  })
  events.onerror = event => { if (events.readyState === EventSource.CLOSED) onError(event) }
  return () => events.close()
}
