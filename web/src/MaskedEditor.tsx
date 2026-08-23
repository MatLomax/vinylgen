import { FormEvent, KeyboardEvent, PointerEvent, useEffect, useRef, useState } from 'react'
import { api, type Artifact, type MaskedEditCandidates } from './api'

type Props = { projectId: string; revisionId: string; source: Artifact; onApply: (revisionId: string) => void }

export function MaskedEditor({ projectId, revisionId, source, onApply }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const drawing = useRef(false)
  const previous = useRef<{ x: number; y: number } | null>(null)
  const [brush, setBrush] = useState(36)
  const [erase, setErase] = useState(false)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<MaskedEditCandidates | null>(null)
  const [showFlattened, setShowFlattened] = useState(false)
  const [error, setError] = useState('')
  const [activity, setActivity] = useState('')
  const [size, setSize] = useState({ width: 3, height: 2 })
  const [cursor, setCursor] = useState({ x: 0, y: 0 })

  useEffect(() => {
    const image = new Image()
    image.onload = () => {
      const target = canvas.current
      if (!target) return
      target.width = image.naturalWidth; target.height = image.naturalHeight
      setSize({ width: image.naturalWidth, height: image.naturalHeight })
      setCursor({ x: Math.floor(image.naturalWidth / 2), y: Math.floor(image.naturalHeight / 2) })
      target.getContext('2d')?.clearRect(0, 0, target.width, target.height)
    }
    image.onerror = () => setError('Could not load the source image')
    image.src = source.download_url
    setResult(null); setActivity('')
  }, [source.id, revisionId])

  function point(event: PointerEvent<HTMLCanvasElement>) {
    const bounds = event.currentTarget.getBoundingClientRect()
    return { x: (event.clientX - bounds.left) * event.currentTarget.width / bounds.width,
      y: (event.clientY - bounds.top) * event.currentTarget.height / bounds.height }
  }
  function stroke(event: PointerEvent<HTMLCanvasElement>) {
    const context = event.currentTarget.getContext('2d')!
    const p = point(event)
    context.globalCompositeOperation = erase ? 'destination-out' : 'source-over'
    context.strokeStyle = '#ffffff'; context.lineWidth = brush; context.lineCap = 'round'
    context.beginPath(); context.moveTo(previous.current?.x ?? p.x, previous.current?.y ?? p.y)
    context.lineTo(p.x, p.y); context.stroke(); previous.current = p
  }
  function down(event: PointerEvent<HTMLCanvasElement>) {
    event.currentTarget.setPointerCapture(event.pointerId); drawing.current = true; previous.current = null; stroke(event)
  }
  function move(event: PointerEvent<HTMLCanvasElement>) { if (drawing.current) stroke(event) }
  function up() { drawing.current = false; previous.current = null }
  function clear() { const target = canvas.current; if (target) target.getContext('2d')?.clearRect(0, 0, target.width, target.height) }
  function dab() {
    const target = canvas.current
    if (!target) return
    const context = target.getContext('2d')!
    context.globalCompositeOperation = erase ? 'destination-out' : 'source-over'
    context.fillStyle = '#ffffff'
    context.beginPath(); context.arc(cursor.x, cursor.y, brush / 2, 0, Math.PI * 2); context.fill()
  }
  function keyDown(event: KeyboardEvent<HTMLCanvasElement>) {
    const directions: Partial<Record<string, [number, number]>> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }
    const direction = directions[event.key]
    if (direction) {
      event.preventDefault()
      const step = event.shiftKey ? 10 : 1
      setCursor(({ x, y }) => ({ x: Math.max(0, Math.min(size.width - 1, x + direction[0] * step)), y: Math.max(0, Math.min(size.height - 1, y + direction[1] * step)) }))
    } else if (event.key === ' ') {
      event.preventDefault(); dab()
    }
  }

  async function request(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError(''); setActivity('Generating masked edit candidates…')
    const form = new FormData(event.currentTarget)
    const prompt = String(form.get('replacement') || '').trim()
    const submitter = (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null
    const mode = submitter?.value === 'remove' ? 'remove' : 'replace'
    if (!prompt) { setBusy(false); setActivity(''); return setError('Describe what should replace or be removed') }
    try {
      const blob = await new Promise<Blob>((resolve, reject) => canvas.current!.toBlob(value => value ? resolve(value) : reject(new Error('Could not encode mask')), 'image/png'))
      const next = await api.maskedEdit(projectId, revisionId, blob, prompt, mode,
        Number(form.get('count')) as 1 | 3, Number(form.get('padding')))
      setResult(next); setActivity(`${next.candidates.length} ${next.candidates.length === 1 ? 'candidate' : 'candidates'} ready.`)
    } catch (cause) { setError(String(cause)); setActivity('') } finally { setBusy(false) }
  }
  async function apply(index: number) {
    if (!result) return
    setBusy(true); setError(''); setActivity('Applying candidate…')
    try { onApply((await api.applyMaskedEdit(projectId, result.revision.id, index)).id); setActivity('Candidate applied.') }
    catch (cause) { setError(String(cause)); setActivity(''); setBusy(false) }
  }

  return <section className="masked-editor">
    <div className="editor-head"><div><h2>Masked generative replacement</h2><p>Paint the exact editable area. Pixels outside it are preserved mechanically.</p></div><span>Semantic results still require review.</span></div>
    <div className="mask-layout">
      <div><p id="mask-keyboard-help">Keyboard: arrows move the mask cursor (Shift moves 10 pixels); Space paints or erases one brush-sized area.</p><div className="mask-canvas" style={{ backgroundImage: `url(${source.download_url})`, aspectRatio: `${size.width} / ${size.height}` }}><canvas ref={canvas} tabIndex={0} aria-label="Masked edit painting canvas" aria-describedby="mask-keyboard-help mask-cursor-status" aria-keyshortcuts="ArrowUp ArrowDown ArrowLeft ArrowRight Shift+ArrowUp Shift+ArrowDown Shift+ArrowLeft Shift+ArrowRight Space" onKeyDown={keyDown} onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up} /></div><p id="mask-cursor-status" role="status" aria-live="polite">Keyboard cursor: x {cursor.x}, y {cursor.y} · {erase ? 'erase' : 'paint'} mode</p></div>
      <form className="mask-controls" onSubmit={request}>
        <div className="tool-grid"><button type="button" className={!erase ? 'active' : ''} onClick={() => setErase(false)}>Paint mask</button><button type="button" className={erase ? 'active' : ''} onClick={() => setErase(true)}>Erase</button></div>
        <label>Brush size · {brush}px<input type="range" min="4" max="256" value={brush} onChange={event => setBrush(Number(event.target.value))} /></label>
        <button type="button" className="secondary" onClick={clear}>Clear mask</button>
        <label>Short replacement request<textarea name="replacement" placeholder="e.g. a brass observation dome" required /></label>
        <div className="form-pair"><label>Candidates<select name="count" defaultValue="1"><option value="1">One</option><option value="3">Three</option></select></label><label>Crop padding<input name="padding" type="number" min="0" max="1024" defaultValue="64" /></label></div>
        <div className="mask-actions"><button disabled={busy} type="submit" value="replace">{result ? 'Regenerate' : 'Generate replacement'}</button><button disabled={busy} type="submit" value="remove" className="secondary">Remove selection</button></div>
        <p role="status" aria-live="polite" aria-atomic="true">{activity}</p>
        {error && <p className="error" role="alert">{error}</p>}
      </form>
    </div>
    {result && <div className="candidate-review"><div className="editor-head"><div><h2>Candidates</h2><p>{result.expanded_prompt}</p></div><label className="preview-toggle"><input type="checkbox" checked={showFlattened} onChange={event => setShowFlattened(event.target.checked)} /> Flatten previews</label></div><div className="candidate-grid">{result.candidates.map((candidate, index) => <article key={candidate.source.id}><img src={(showFlattened ? candidate.flattened_preview : candidate.source).download_url} alt={`Masked edit candidate ${index + 1}`} /><button disabled={busy} onClick={() => apply(index + 1)}>Apply candidate {index + 1}</button></article>)}</div></div>}
  </section>
}
