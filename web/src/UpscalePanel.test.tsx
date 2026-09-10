// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, type UpscaleOptions, type UpscaleSettings } from './api'
import { UpscalePanel } from './UpscalePanel'

const DEFAULTS: UpscaleSettings = {
  upscaler: '4xNomos8kSCHAT-S.pth', upscale_factor: 2, checkpoint: 'blue_pencil-XL-v7.0.0.safetensors',
  denoise: 0.7, controlnet: true, controlnet_strength: 0.4, sample_size: 1024, tile_size: 512, seed: null,
}
const OPTIONS: UpscaleOptions = {
  upscalers: [{ value: '4xNomos8kSCHAT-S.pth', label: 'Nomos 8k SC HAT' }, { value: 'none', label: 'None (plain resize)' }],
  checkpoints: [{ value: 'blue_pencil-XL-v7.0.0.safetensors', label: 'blue_pencil XL v7' }],
  controlnets: [{ value: 'control_v11f1e_sd15_tile.pth', label: 'Tile ControlNet' }],
  defaults: DEFAULTS, sample_size_max: 1024,
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('upscale settings panel', () => {
  it('seeds the form from the fetched defaults', async () => {
    vi.spyOn(api, 'upscaleOptions').mockResolvedValue(OPTIONS)
    render(<UpscalePanel open projectId="project-1" revisionId="revision-1" onClose={() => undefined} onStarted={() => undefined} />)

    const upscaler = await screen.findByLabelText(/^Upscaler/) as HTMLSelectElement
    expect(upscaler.value).toBe(DEFAULTS.upscaler)
    const factor = screen.getByLabelText(/Source detail upscale/) as HTMLInputElement
    expect(Number(factor.value)).toBe(DEFAULTS.upscale_factor)
    const denoise = screen.getByLabelText(/^Denoise/) as HTMLInputElement
    expect(Number(denoise.value)).toBe(DEFAULTS.denoise)
    const controlnetStrength = screen.getByLabelText(/^ControlNet strength/) as HTMLInputElement
    expect(Number(controlnetStrength.value)).toBe(DEFAULTS.controlnet_strength)
  })

  it('posts the complete settings to the revision upscale endpoint on submit', async () => {
    vi.spyOn(api, 'upscaleOptions').mockResolvedValue(OPTIONS)
    const upscale = vi.spyOn(api, 'upscale').mockResolvedValue({ project: {} as never, job: { id: 'job-1' } as never })
    const onStarted = vi.fn()
    render(<UpscalePanel open projectId="project-1" revisionId="revision-1" onClose={() => undefined} onStarted={onStarted} />)

    await screen.findByLabelText(/^Upscaler/)
    fireEvent.click(screen.getByRole('button', { name: 'Start upscale' }))

    await vi.waitFor(() => expect(upscale).toHaveBeenCalledWith('project-1', 'revision-1', DEFAULTS))
    expect(onStarted).toHaveBeenCalledWith({ id: 'job-1' })
  })

  it('disables the factor slider once the upscaler is set to none', async () => {
    vi.spyOn(api, 'upscaleOptions').mockResolvedValue(OPTIONS)
    render(<UpscalePanel open projectId="project-1" revisionId="revision-1" onClose={() => undefined} onStarted={() => undefined} />)

    const upscaler = await screen.findByLabelText(/^Upscaler/) as HTMLSelectElement
    const factor = screen.getByLabelText(/Source detail upscale/) as HTMLInputElement
    expect(factor.disabled).toBe(false)

    fireEvent.change(upscaler, { target: { value: 'none' } })

    expect(factor.disabled).toBe(true)
  })

  it('renders nothing when closed', () => {
    vi.spyOn(api, 'upscaleOptions').mockResolvedValue(OPTIONS)
    render(<UpscalePanel open={false} projectId="project-1" revisionId="revision-1" onClose={() => undefined} onStarted={() => undefined} />)
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
