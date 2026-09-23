// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { growToFit } from './App'

function textarea(offsetHeight: number, clientHeight: number, scrollHeight: number): HTMLTextAreaElement {
  const element = document.createElement('textarea')
  Object.defineProperty(element, 'offsetHeight', { value: offsetHeight })
  Object.defineProperty(element, 'clientHeight', { value: clientHeight })
  Object.defineProperty(element, 'scrollHeight', { value: scrollHeight })
  return element
}

describe('growToFit', () => {
  it('grows the textarea to fit its text, including the border', () => {
    const element = textarea(88, 86, 140)
    growToFit(element)
    expect(element.style.height).toBe('142px')
  })

  it('never shrinks a textarea that already fits, keeping a manual resize', () => {
    const element = textarea(200, 198, 60)
    growToFit(element)
    expect(element.style.height).toBe('')
  })

  it('ignores a missing element', () => {
    expect(() => growToFit(null)).not.toThrow()
  })
})
