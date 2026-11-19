import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { sanitizePreviewText } from './preview-text'

// Shared with the backend's `tests/session_rules/test_preview_text.py`, so the server preview and
// this renderer twin can never disagree about what a list row shows.
const FIXTURE = join(dirname(fileURLToPath(import.meta.url)), '../../../../tests/fixtures/preview_text_cases.json')

const { cases } = JSON.parse(readFileSync(FIXTURE, 'utf8')) as {
  cases: { expected: string; input: string; name: string }[]
}

describe('sanitizePreviewText', () => {
  it('has shared cases to run', () => {
    expect(cases.length).toBeGreaterThan(10)
  })

  it.each(cases.map(c => [c.name, c] as const))('%s', (_name, c) => {
    expect(sanitizePreviewText(c.input)).toBe(c.expected)
  })

  it('reads non-strings as empty', () => {
    expect(sanitizePreviewText(null)).toBe('')
    expect(sanitizePreviewText(undefined)).toBe('')
  })
})
