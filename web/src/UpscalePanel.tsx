import { FormEvent, useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { api, type Job, type UpscaleOptions, type UpscaleSettings } from './api'

import { Field, NumberField, Slider } from './PipelineControls'

type AvailableOptions = Extract<UpscaleOptions, { available: true }>

function message(cause: unknown): string { return cause instanceof Error ? cause.message : String(cause) }

// A manual, optional trigger on a source revision (docs/upscale-stage.md section 7): a
// small settings surface for the pre-flatten generative tiled-detail upscale, seeded
// from /api/upscale/options and submitted against the chosen revision. The returned
// job is handed to the caller, which follows it with the existing SSE progress overlay.
export function UpscalePanel({ open, projectId, revisionId, onClose, onStarted }: {
  open: boolean
  projectId: string
  revisionId: string
  onClose: () => void
  onStarted: (job: Job) => void
}) {
  const [options, setOptions] = useState<AvailableOptions | null>(null)
  const [unavailable, setUnavailable] = useState(false)
  const [settings, setSettings] = useState<UpscaleSettings | null>(null)
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) return
    setError('')
    setOptions(null)
    setUnavailable(false)
    setSettings(null)
    setLoading(true)
    const controller = new AbortController()
    api.upscaleOptions(controller.signal).then(loaded => {
      if (!loaded.available) { setUnavailable(true); return }
      setOptions(loaded)
      setSettings(loaded.defaults)
    }).catch(cause => { if (!controller.signal.aborted) setError(message(cause)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [open])

  useEffect(() => {
    if (!open) return
    const keyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    document.addEventListener('keydown', keyDown)
    return () => document.removeEventListener('keydown', keyDown)
  }, [open, onClose])

  if (!open) return null

  function set<K extends keyof UpscaleSettings>(key: K, value: UpscaleSettings[K]) {
    setSettings(current => current ? { ...current, [key]: value } : current)
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!settings) return
    setSubmitting(true)
    setError('')
    try {
      const started = await api.upscale(projectId, revisionId, settings)
      onStarted(started.job)
    } catch (cause) { setError(message(cause)) }
    finally { setSubmitting(false) }
  }

  return <div className="upscale-modal-shell" role="dialog" aria-modal="true" aria-labelledby="upscale-modal-title">
    <div className="upscale-modal">
      <div className="upscale-modal-head">
        <h2 id="upscale-modal-title">Upscale source</h2>
        <button type="button" className="secondary icon-button" aria-label="Close" onClick={onClose}><X aria-hidden="true" /></button>
      </div>
      {!unavailable && <p className="section-intro">Run a generative tiled-detail redraw on this source before flattening — a locally-running ComfyUI adds real resolution and detail. This creates a new, recoverable revision and can take a while.</p>}
      {loading && <p role="status">Loading upscale options…</p>}
      {!loading && unavailable && <p className="error" role="alert">ComfyUI is not running.</p>}
      {!loading && settings && options && <form onSubmit={submit}>
        <div className="settings-grid">
          <Field label="Upscaler" help="Upscale model run before the tiled redraw, or none / lanczos for a plain resize with no redraw detail.">
            <select value={settings.upscaler} onChange={event => set('upscaler', event.target.value)}>
              {options.upscalers.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </Field>
          <Slider label="Source detail upscale (×)" help="Continuous scale applied to the raw source before the tiled redraw. Distinct from the post-flatten Output upscale — this adds real detail rather than nearest-neighbour scaling." value={settings.upscale_factor} min={1} max={4} step={.25} disabled={settings.upscaler === 'none'} format={value => `${value.toFixed(2)}×`} onChange={value => set('upscale_factor', value)} />
          <Slider label="Denoise" help="How strongly the tiled redraw is allowed to change the upscaled image, from a light touch-up to a heavy redraw." value={settings.denoise} min={0} max={1} step={.01} format={value => value.toFixed(2)} onChange={value => set('denoise', value)} />
          <label className="check-field"><input type="checkbox" checked={settings.controlnet} onChange={event => set('controlnet', event.target.checked)} /><span><strong>ControlNet</strong><small>Keep the tiled redraw structurally locked to the source with a tile ControlNet.</small></span></label>
          <Slider label="ControlNet strength" help="How tightly the redraw follows the source structure." value={settings.controlnet_strength} min={0} max={2} step={.05} format={value => value.toFixed(2)} disabled={!settings.controlnet} onChange={value => set('controlnet_strength', value)} />
        </div>
        <details className="advanced-settings">
          <summary>Advanced<small>Checkpoint &amp; sample size</small></summary>
          <div className="settings-grid">
            <Field label="Checkpoint" help="Diffusion checkpoint used for the tiled redraw.">
              <select value={settings.checkpoint} onChange={event => set('checkpoint', event.target.value)}>
                {options.checkpoints.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            </Field>
            <NumberField label="Sample size" help={`Tile sample size in pixels, capped at ${options.sample_size_max} by available VRAM.`} value={settings.sample_size} min={64} max={options.sample_size_max} step={1} onChange={value => set('sample_size', value ?? settings.sample_size)} />
          </div>
        </details>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="upscale-modal-actions">
          <button type="button" className="secondary" onClick={onClose}>Cancel</button>
          <button type="submit" disabled={submitting}>Start upscale</button>
        </div>
      </form>}
      {!loading && !settings && error && <p className="error" role="alert">{error}</p>}
    </div>
  </div>
}
