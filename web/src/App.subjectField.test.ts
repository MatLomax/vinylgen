// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  projects: vi.fn(), project: vi.fn(), effectiveArtifacts: vi.fn(), artifacts: vi.fn(), editor: vi.fn(),
  inkscapeHandoff: vi.fn(), health: vi.fn(), followJob: vi.fn(),
}))
vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>()
  return { ...original, api: { ...original.api, ...mocks }, followJob: mocks.followJob }
})

// jsdom has no layout: every textarea reports a 2 px border and 140 px of text.
const layout = { offsetHeight: 88, clientHeight: 86, scrollHeight: 140 }

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  window.history.replaceState(null, '', '/')
  window.scrollTo = vi.fn()
  for (const [name, value] of Object.entries(layout)) {
    Object.defineProperty(HTMLTextAreaElement.prototype, name, { configurable: true, get: () => value })
  }
  mocks.projects.mockResolvedValue([])
  mocks.health.mockResolvedValue({ status: 'ok', max_upload_bytes: 100 * 1024 * 1024 })
  mocks.followJob.mockReturnValue(() => undefined)
})
afterEach(() => {
  cleanup()
  for (const name of Object.keys(layout)) delete (HTMLTextAreaElement.prototype as unknown as Record<string, unknown>)[name]
})

describe('Short subject field', () => {
  it('grows to fit its text on mount, focus and input', async () => {
    render(createElement((await import('./App')).default))
    const field = await screen.findByLabelText('Short subject') as HTMLTextAreaElement
    expect(field.style.height).toBe('142px')
    field.style.height = ''
    fireEvent.focus(field)
    expect(field.style.height).toBe('142px')
    field.style.height = ''
    fireEvent.input(field, { target: { value: 'a much longer subject' } })
    expect(field.style.height).toBe('142px')
  })
})
