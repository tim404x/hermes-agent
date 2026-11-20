// Desktop polish batch 1, hydration half: the row-level rules the thread test above renders.
import { describe, expect, it } from 'vitest'

import type { SessionMessage } from '@/types/hermes'

import fixture from './__fixtures__/polish-1119-real-rows.json'
import { chatMessageText } from './parts'

import { toChatMessages } from '.'

const rows = (slice: keyof Omit<typeof fixture, '_doc'>) => fixture[slice] as unknown as SessionMessage[]

describe('polish 1119 hydration', () => {
  it('a stored interrupt sentinel paints nothing, and the turn still reads as stopped', () => {
    const messages = toChatMessages([
      { role: 'user', content: 'send it', timestamp: 1 },
      {
        role: 'assistant',
        content: 'Operation interrupted: waiting for model response (0.3s elapsed).',
        display_metadata: { interrupted: true },
        timestamp: 2
      }
    ] as SessionMessage[])

    expect(messages.map(chatMessageText).join('\n')).not.toContain('Operation interrupted')
  })

  it('cron output and generic runtime notifications hydrate as system events with a label', () => {
    const [cron] = toChatMessages(rows('forge_cron'))

    expect(cron.role).toBe('system')
    expect(chatMessageText(cron)).toBe('🤖 Team report | Forge')
  })

  it('a cron row the backend already typed reads the same from the REST page and the resume projection', () => {
    const typed = {
      role: 'user',
      display_kind: 'internal_notification',
      display_metadata: { cron_delivery: { job_name: '🤖 Team report | Forge' }, display_text: '🤖 Team report | Forge' },
      timestamp: 1
    }

    for (const shape of [
      { content: '[Cronjob "🤖 Team report | Forge" output — scheduled job]\n\nMorning run is done.', display_content: 'Morning run is done.' },
      { text: 'Morning run is done.' }
    ]) {
      const [row] = toChatMessages([{ ...typed, ...shape } as SessionMessage])

      expect(row.role).toBe('system')
      expect(chatMessageText(row)).toBe('🤖 Team report | Forge')
      expect(row.asyncResult).toBe('Morning run is done.')
    }
  })
})
