import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { isSilenceMarkerText, stripTrailingSilenceMarker } from './silence-marker'

// Shared with the gateway's `tests/gateway/test_response_filters.py`, so the delivery rule and
// this paint rule can never disagree about what a silence marker is.
const FIXTURE = join(dirname(fileURLToPath(import.meta.url)), '../../../../tests/fixtures/silence_marker_cases.json')

const { cases } = JSON.parse(readFileSync(FIXTURE, 'utf8')) as {
  cases: { input: string; name: string; silent: boolean; strip?: string }[]
}

describe('isSilenceMarkerText', () => {
  it('has shared cases to run', () => {
    expect(cases.length).toBeGreaterThan(10)
  })

  it.each(cases.map(c => [c.name, c] as const))('%s', (_name, c) => {
    expect(isSilenceMarkerText(c.input)).toBe(c.silent)

    if (c.strip !== undefined) {
      expect(stripTrailingSilenceMarker(c.input)).toBe(c.strip)
    }
  })

  it('reads non-strings as not silent', () => {
    expect(isSilenceMarkerText(null)).toBe(false)
    expect(isSilenceMarkerText(undefined)).toBe(false)
  })
})

describe('stripTrailingSilenceMarker', () => {
  it('drops a marker closing a real reply on its own line', () => {
    expect(stripTrailingSilenceMarker('Paris.\n\nNO_REPLY')).toBe('Paris.')
    expect(stripTrailingSilenceMarker('Shipped it.\n[SILENT] ⚡\n')).toBe('Shipped it.')
  })

  it('keeps a bare marker (the silence verdict) and a marker inside a line', () => {
    expect(stripTrailingSilenceMarker('NO_REPLY')).toBe('NO_REPLY')
    expect(stripTrailingSilenceMarker('Paris.\nI said NO_REPLY to the ping.')).toBe(
      'Paris.\nI said NO_REPLY to the ping.'
    )
  })
})
