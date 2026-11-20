import { describe, expect, it } from 'vitest'

import type { SessionMessage } from '@/types/hermes'

import { toChatMessages } from './hydration'

// A lean transcript page (`image_refs=true`, api/sessions.ts setLeanTranscriptParams) ships a
// pasted screenshot's user row without its inline base64 copy: only the text part, whose
// `@image:<path>` line the attachments row loads on demand. Hydration must lift that line
// into a clean ref, exactly like a string-content row, so the image still renders once.
describe('lean page image rows', () => {
  const shot = '/home/hermes/.hermes/profiles/kepler/images/upload_zj9f2sl5.png'

  const row = (content: SessionMessage['content']): SessionMessage =>
    ({ content, id: 7, role: 'user', timestamp: 1795214659 }) as SessionMessage

  it('lifts the reference of a parts-list row whose inline copy was dropped', () => {
    const [message] = toChatMessages([row([{ text: `why 3D to 2D?\n@image:${shot}`, type: 'text' }])])

    expect(message.attachmentRefs).toEqual([`@image:${shot}`])
    expect(message.parts).toEqual([expect.objectContaining({ text: 'why 3D to 2D?\n', type: 'text' })])
    expect(JSON.stringify(message)).not.toContain('data:image')
  })

  it('hydrates the same as the string-content row of the same prompt', () => {
    const [lean] = toChatMessages([row([{ text: `why 3D to 2D?\n@image:${shot}`, type: 'text' }])])
    const [flat] = toChatMessages([row(`why 3D to 2D?\n@image:${shot}`)])

    expect(lean.attachmentRefs).toEqual(flat.attachmentRefs)
    expect(lean.parts).toEqual(flat.parts)
  })
})
