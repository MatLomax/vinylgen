export type Project = { id: string; slug: string; name: string; created_at: string; updated_at: string }
export type Revision = { id: string; project_id: string; parent_revision_id: string | null; kind: string; message: string | null; settings: Record<string, unknown>; created_at: string }
export type Artifact = { id: string; revision_id: string; kind: string; filename: string; media_type: string; byte_size: number; download_url: string }
export type Job = { id: string; project_id: string | null; revision_id: string | null; result_revision_id: string | null; kind: string; status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'; progress: number; message: string | null; error: string | null }
export type PaletteColour = { index: number; name: string; hex: string; rgb: [number, number, number]; share_percent: number }
export type EditorState = { width: number; height: number; palette: PaletteColour[]; protected_pixels: number }
export type EditOperation = { tool: 'pencil' | 'brush' | 'flood_fill' | 'lasso' | 'component' | 'protect' | 'merge_colour' | 'rename_colour' | 'adjust_colour'; label?: number; source_label?: number; target_label?: number; points?: [number, number][]; radius?: number; name?: string; rgb?: [number, number, number] }
export type GenerationSettings = { width: number; height: number; preset: 'simple' | 'balanced' | 'detailed'; quality: 'low' | 'medium' | 'high'; required_motifs: string[]; rejected_motifs: string[] }
export type GenerationStarted = { project: Project; job: Job; expanded_prompt: string }
export type MaskedEditCandidate = { source: Artifact; flattened_preview: Artifact }
export type MaskedEditCandidates = { revision: Revision; expanded_prompt: string; candidates: MaskedEditCandidate[] }
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
  maskedEdit: (projectId: string, revisionId: string, mask: Blob, prompt: string,
    mode: 'replace' | 'remove', count: 1 | 3, padding: number) => {
    const body = new FormData()
    body.append('mask', mask, 'mask.png'); body.append('prompt', prompt); body.append('mode', mode)
    body.append('count', String(count)); body.append('padding', String(padding))
    return fetch(`/api/projects/${projectId}/revisions/${revisionId}/masked-edits`, { method: 'POST', body }).then(result<MaskedEditCandidates>)
  },
  applyMaskedEdit: (projectId: string, candidateRevisionId: string, candidateIndex: number) =>
    fetch(`/api/projects/${projectId}/masked-edits/apply`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ candidate_revision_id: candidateRevisionId, candidate_index: candidateIndex }) }).then(result<Revision>),
  inkscapeHandoff: (revisionId: string) => fetch(`/api/revisions/${revisionId}/inkscape-handoff`).then(result<InkscapeHandoff>),
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
  startPipeline: (kind: 'palette' | 'trace' | 'validate' | 'export', projectId: string, revisionId: string) => fetch('/api/jobs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, project_id: projectId, revision_id: revisionId }),
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
  events.onerror = onError
  return () => events.close()
}
