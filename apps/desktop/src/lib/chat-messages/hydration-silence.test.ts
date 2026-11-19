import { describe, expect, it } from 'vitest'

import { toChatMessages } from './hydration'
import { chatMessageText } from './parts'

const PROCESS_DONE =
  '[IMPORTANT: Background process proc_74ccce87377c completed normally (exit code 0).\nCommand: cd ~/.hermes/work/deal-heatmap/element; timeout 900 python3 build.py\nOutput:\nok\n]'

const processRow = (timestamp: number) => ({
  role: 'user',
  content: PROCESS_DONE,
  timestamp,
  display_kind: 'process_complete',
  display_metadata: {
    display_text: 'Background Process Finished: cd ~/.hermes/work/deal-heatmap/element; timeout 900 p...'
  }
})

const painted = (messages: unknown[]) =>
  toChatMessages(messages as never).map(message => ({ role: message.role, text: chatMessageText(message).trim() }))

// A bot that has nothing to add ends its turn with a silence marker; the stored row keeps the model's
// text. Painting stored rows must swallow it the way the gateway swallows it on delivery, and a silent
// background wake must leave no trace in the thread.
describe('toChatMessages with silence markers', () => {
  it('hides a NO_REPLY turn and the background-process row that woke it', () => {
    const result = painted([
      { role: 'user', content: 'build the heatmap', timestamp: 100 },
      { role: 'assistant', content: 'The heatmap is ready.', timestamp: 101 },
      processRow(200),
      { role: 'assistant', content: 'NO_REPLY', timestamp: 201, reasoning: 'nothing new for Tim' }
    ])

    expect(result).toEqual([
      { role: 'user', text: 'build the heatmap' },
      { role: 'assistant', text: 'The heatmap is ready.' }
    ])
  })

  it.each(['NO_REPLY.', '[SILENT]', ' no_reply ', 'NO_REPLY ⚡'])('treats %j as silence', marker => {
    const result = painted([
      { role: 'user', content: 'hi', timestamp: 100 },
      processRow(200),
      { role: 'assistant', content: marker, timestamp: 201 }
    ])

    expect(result).toEqual([{ role: 'user', text: 'hi' }])
  })

  it('keeps a background-process row whose turn said something', () => {
    const result = painted([
      processRow(200),
      { role: 'assistant', content: 'Build finished: 12 deals.', timestamp: 201 }
    ])

    expect(result.map(m => m.role)).toEqual(['system', 'assistant'])
    expect(result[1].text).toBe('Build finished: 12 deals.')
  })

  it('keeps a background-process row whose turn ran tools, and still hides the marker', () => {
    const result = painted([
      processRow(200),
      {
        role: 'assistant',
        content: '',
        timestamp: 201,
        tool_calls: [{ id: 'c1', function: { name: 'terminal', arguments: '{"command":"ls"}' } }]
      },
      { role: 'tool', content: 'out', tool_call_id: 'c1', tool_name: 'terminal', timestamp: 202 },
      { role: 'assistant', content: 'NO_REPLY', timestamp: 203 }
    ])

    expect(result[0].role).toBe('system')
    expect(result.some(m => m.text.includes('NO_REPLY'))).toBe(false)
  })

  it('hides a bare marker answering a human turn but keeps the human message', () => {
    expect(
      painted([
        { role: 'user', content: 'anything new?', timestamp: 100 },
        { role: 'assistant', content: '[SILENT]', timestamp: 101 }
      ])
    ).toEqual([{ role: 'user', text: 'anything new?' }])
  })

  it('keeps a reply that merely mentions the marker', () => {
    const result = painted([
      processRow(200),
      { role: 'assistant', content: 'I answered NO_REPLY to the ping on purpose.', timestamp: 201 }
    ])

    expect(result.map(m => m.text)).toContain('I answered NO_REPLY to the ping on purpose.')
    expect(result[0].role).toBe('system')
  })

  it('drops a marker that closes a real reply on its own line', () => {
    const result = painted([
      { role: 'user', content: 'capital of France?', timestamp: 100 },
      { role: 'assistant', content: 'Paris.\n\nNO_REPLY', timestamp: 101 }
    ])

    expect(result[1]).toEqual({ role: 'assistant', text: 'Paris.' })
  })

  it('keeps a background-process row whose turn has not been stored yet', () => {
    expect(painted([{ role: 'user', content: 'hi', timestamp: 100 }, processRow(200)]).map(m => m.role)).toEqual([
      'user',
      'system'
    ])
  })
})
