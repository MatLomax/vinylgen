import { FormEvent, useEffect, useState, type CSSProperties } from 'react'
import { api, followJob, type Artifact, type InkscapeHandoff, type Job, type Project, type Revision } from './api'
import { ImageWorkspace, type WorkspaceLayer } from './ImageWorkspace'
import { FlattenedEditor } from './FlattenedEditor'
import { MaskedEditor } from './MaskedEditor'
import './styles.css'

const MASK_ARTIFACT_KINDS = new Set(['edit-mask'])
const OVERLAY_ARTIFACT_KINDS = new Set([
  'trace-fallbacks',
  'trace-semantic-warnings',
])

export function artifactToWorkspaceLayer(artifact: Artifact): WorkspaceLayer | null {
  if (!artifact.media_type.startsWith('image/')) return null
  const role = MASK_ARTIFACT_KINDS.has(artifact.kind) ? 'mask'
    : OVERLAY_ARTIFACT_KINDS.has(artifact.kind) ? 'overlay'
      : 'image'
  return {
    id: artifact.id,
    name: `${artifact.kind} · ${artifact.filename}`,
    url: artifact.download_url,
    mediaType: artifact.media_type,
    role,
  }
}

export default function App() {
  const [projects, setProjects] = useState<Project[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const [revision, setRevision] = useState<string | null>(null)
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [revisions, setRevisions] = useState<Revision[]>([])
  const [error, setError] = useState('')
  const [expandedPrompt, setExpandedPrompt] = useState('')
  const [handoff, setHandoff] = useState<InkscapeHandoff | null>(null)
  const [handoffMessage, setHandoffMessage] = useState('')

  useEffect(() => { api.projects().then(setProjects).catch(e => setError(String(e))) }, [])
  useEffect(() => job ? followJob(job.id, next => {
    setJob(next)
    if (next.status === 'succeeded' && next.result_revision_id) setRevision(next.result_revision_id)
  }, () => setError('Lost the live job connection')) : undefined, [job?.id])
  useEffect(() => {
    if (!selected) { setArtifacts([]); setRevision(null); setRevisions([]); return }
    const controller = new AbortController()
    setArtifacts([])
    api.project(selected, controller.signal).then(async project => {
      setRevisions(project.revisions)
      const latest = revision && project.revisions.some(row => row.id === revision)
        ? revision : project.revisions[0]?.id
      setRevision(latest || null)
      setArtifacts(latest ? await api.effectiveArtifacts(latest, controller.signal) : [])
    }).catch(cause => {
      if (!controller.signal.aborted) setError(String(cause))
    })
    return () => controller.abort()
  }, [selected, revision])
  useEffect(() => {
    setHandoff(null); setHandoffMessage('')
    if (revision && artifacts.some(row => row.media_type === 'image/svg+xml')) {
      api.inkscapeHandoff(revision).then(setHandoff).catch(cause => setError(String(cause)))
    }
  }, [revision, artifacts.map(row => row.id).join(',')])

  async function importImage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    const form = new FormData(event.currentTarget)
    const file = form.get('source') as File
    if (!file?.size) return setError('Choose an image to import')
    try {
      const project = await api.importProject(String(form.get('name') || file.name.replace(/\.[^.]+$/, '')), file)
      setProjects(current => [project, ...current])
      setSelected(project.id)
      setRevision(project.revisions[0].id)
      event.currentTarget.reset()
    } catch (cause) { setError(String(cause)) }
  }

  async function generateImage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    const form = new FormData(event.currentTarget)
    const prompt = String(form.get('prompt') || '').trim()
    if (!prompt) return setError('Enter a subject to generate')
    const ratio = String(form.get('ratio'))
    const dimensions = ratio === 'portrait' ? [1024, 1536] : ratio === 'square' ? [1024, 1024]
      : ratio === 'custom' ? [Number(form.get('width')), Number(form.get('height'))] : [1536, 1024]
    const list = (name: string) => String(form.get(name) || '').split(',').map(value => value.trim()).filter(Boolean)
    try {
      const started = await api.generate(prompt, String(form.get('project_name') || ''), {
        width: dimensions[0], height: dimensions[1],
        preset: String(form.get('preset')) as 'simple' | 'balanced' | 'detailed',
        quality: String(form.get('quality')) as 'low' | 'medium' | 'high',
        required_motifs: list('required'), rejected_motifs: list('rejected'),
      })
      setExpandedPrompt(started.expanded_prompt)
      setProjects(current => [started.project, ...current])
      setSelected(started.project.id)
      setJob(started.job)
    } catch (cause) { setError(String(cause)) }
  }

  async function runPipeline(kind: 'palette' | 'trace' | 'validate' | 'export') {
    if (!selected || !revision) return
    setError('')
    try { setJob(await api.startPipeline(kind, selected, revision)) }
    catch (cause) { setError(String(cause)) }
  }
  async function reimportSvg(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selected || !revision) return
    const form = new FormData(event.currentTarget)
    const file = form.get('svg') as File
    if (!file?.size) return setError('Choose an edited SVG to reimport')
    try {
      const next = await api.reimportInkscape(selected, revision, file)
      setRevision(next.id); setHandoffMessage('Edited SVG validated and saved as a new revision.')
      event.currentTarget.reset()
    } catch (cause) { setError(String(cause)) }
  }
  const current = revisions.find(row => row.id === revision)
  const redo = revisions.filter(row => row.parent_revision_id === revision).sort((a, b) => a.created_at.localeCompare(b.created_at)).at(-1)
  const flattened = artifacts.find(row => row.kind === 'flattened')
  const source = artifacts.find(row => row.kind === 'source')
  const jobMessage = job?.status === 'failed'
    ? job.error || job.message || 'Job failed'
    : job?.message || job?.status

  return <main>
    <header><span className="mark">V</span><div><h1>Vinylgen</h1><p>From source image to cuttable layers</p></div></header>
    <section className="workspace">
      <ImageWorkspace layers={artifacts.map(artifactToWorkspaceLayer).filter((layer): layer is WorkspaceLayer => layer !== null)} />
      <aside>
        <h2>Create artwork</h2>
        <form className="generation-form" onSubmit={generateImage}>
          <label>Short subject<textarea name="prompt" placeholder="e.g. sky whale city" required /></label>
          <label>Project name<input name="project_name" placeholder="Defaults to subject" /></label>
          <div className="form-pair"><label>Frame<select name="ratio" defaultValue="landscape"><option value="landscape">Landscape 3:2</option><option value="portrait">Portrait 2:3</option><option value="square">Square 1:1</option><option value="custom">Custom dimensions</option></select></label><label>Art detail<select name="preset" defaultValue="balanced"><option value="simple">Simple</option><option value="balanced">Balanced</option><option value="detailed">Detailed</option></select></label></div>
          <div className="form-pair"><label>Width<input name="width" type="number" min="256" max="4096" defaultValue="1536" /></label><label>Height<input name="height" type="number" min="256" max="4096" defaultValue="1024" /></label></div>
          <label>Quality<select name="quality" defaultValue="medium"><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option></select></label>
          <label>Required motifs<input name="required" placeholder="comma separated" /></label>
          <label>Rejected motifs<input name="rejected" placeholder="comma separated" /></label>
          <button type="submit" disabled={['queued', 'running'].includes(job?.status || '')}>Generate source</button>
        </form>
        {expandedPrompt && <details className="expanded" open><summary>Expanded prompt sent to provider</summary><p>{expandedPrompt}</p></details>}
        <div className="divider"><span>or upload</span></div>
        <form onSubmit={importImage}>
          <label>Name<input name="name" placeholder="Optional project name" /></label>
          <label className="file">Source image<input name="source" type="file" accept="image/*" required /></label>
          <button type="submit">Import image</button>
        </form>
        <h2 className="project-heading">Projects</h2>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="projects">
          {projects.map(project => <button key={project.id} className={selected === project.id ? 'selected' : ''} onClick={() => setSelected(project.id)}>{project.name}<small>{project.slug}</small></button>)}
          {!projects.length && <p className="muted">No projects yet.</p>}
        </div>
        <div className="job">
          <button disabled={!revision || ['queued', 'running'].includes(job?.status || '')} onClick={() => runPipeline('palette')}>Flatten image</button>
          <button disabled={!revision || !artifacts.some(row => row.kind === 'segmentation') || ['queued', 'running'].includes(job?.status || '')} onClick={() => runPipeline('trace')}>Trace and validate</button>
          <button disabled={!revision || !artifacts.some(row => row.kind === 'traced-edge') || ['queued', 'running'].includes(job?.status || '')} onClick={() => runPipeline('validate')}>Revalidate SVGs</button>
          <button disabled={!revision || !artifacts.some(row => row.kind === 'traced-preview') || ['queued', 'running'].includes(job?.status || '')} onClick={() => runPipeline('export')}>Export ZIP</button>
          {job && <><div className="progress" role="progressbar" aria-label="Job progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(job.progress * 100)} aria-valuetext={`${jobMessage} · ${Math.round(job.progress * 100)}%`}><i style={{ '--progress': job.progress } as CSSProperties} /></div><p role={job.status === 'failed' ? 'alert' : 'status'} aria-live={job.status === 'failed' ? 'assertive' : 'polite'} aria-atomic="true">{jobMessage} · {Math.round(job.progress * 100)}%</p>
            {['queued', 'running'].includes(job.status) && <button className="secondary" onClick={() => api.cancelJob(job.id).then(setJob)}>Cancel</button>}</>}
        </div>
      </aside>
    </section>
    {selected && revision && source && <MaskedEditor projectId={selected} revisionId={revision} source={source} onApply={setRevision} />}
    {selected && revision && flattened && <><nav className="history"><button disabled={!current?.parent_revision_id} onClick={() => current?.parent_revision_id && setRevision(current.parent_revision_id)}>Undo</button><select aria-label="Revision" value={revision} onChange={event => setRevision(event.target.value)}>{revisions.map(row => <option key={row.id} value={row.id}>{row.kind} · {new Date(row.created_at).toLocaleString()}</option>)}</select><button disabled={!redo} onClick={() => redo && setRevision(redo.id)}>Redo</button></nav><FlattenedEditor projectId={selected} revisionId={revision} flattened={flattened} onCommit={setRevision} /></>}
    {selected && revision && handoff && <section className="inkscape-handoff"><h2>Export or finish in Inkscape</h2><p>{handoff.guidance}</p><div className="handoff-actions">{handoff.exports.map(item => <span key={item.id}><a className="button-link" href={item.download_url}>Download {item.filename}</a>{handoff.shared_folder_enabled && <button className="secondary" onClick={() => api.saveForInkscape(revision, item.id).then(saved => setHandoffMessage(`Saved ${saved.filename} to the shared folder.`)).catch(cause => setError(String(cause)))}>Save {item.media_type === 'image/png' ? 'PNG' : 'SVG'} to shared folder</button>}</span>)}</div><form onSubmit={reimportSvg}><label className="file">Edited SVG<input name="svg" type="file" accept="image/svg+xml,.svg" required /></label><button type="submit">Validate and reimport</button></form>{handoffMessage && <p className="success" role="status" aria-live="polite">{handoffMessage}</p>}</section>}
  </main>
}
