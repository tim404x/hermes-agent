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

  // Live/reload parity: a person's own turn in a plain session may not end silent
  // (gateway/response_filters.py silence_allowed; tui_gateway/prompt_turn.py delivers the text as
  // written, pinned by test_bot_mode_silence_delivery.py), so the live view painted the reply and a
  // reload must paint the same words. Hiding them is the worst outcome: a real answer vanishes.
  it.each([
    ['what exact token do you end a silent turn with?', '`NO_REPLY`'],
    ['reply with exactly the word Silent.', 'Silent'],
    ['did Dani reply to the email?', 'No reply 😕'],
    ['is the alert loud or silent?', 'Silent.'],
    ['anything new?', '[SILENT]']
  ])('paints the reply to a person %j exactly as the live turn showed it', (question, answer) => {
    expect(
      painted([
        { role: 'user', content: question, timestamp: 100 },
        { role: 'assistant', content: answer, timestamp: 101 }
      ])
    ).toEqual([
      { role: 'user', text: question },
      { role: 'assistant', text: answer }
    ])
  })

  it('hides a silent reply the backend marked silent_turn (a Bot Chat turn), keeping the person', () => {
    expect(
      painted([
        { role: 'user', content: 'thanks, that is all', timestamp: 100 },
        { role: 'assistant', content: 'NO_REPLY ⚡', timestamp: 101, silent_turn: true }
      ])
    ).toEqual([{ role: 'user', text: 'thanks, that is all' }])
  })

  it('lets an unaddressed message end silent, as the gateway does', () => {
    expect(
      painted([
        { role: 'user', content: 'lunch?', timestamp: 100, display_metadata: { reply_expected: false } },
        { role: 'assistant', content: 'NO_REPLY', timestamp: 101 }
      ])
    ).toEqual([{ role: 'user', text: 'lunch?' }])
  })

  it('keeps a reply that merely mentions the marker', () => {
    const result = painted([
      processRow(200),
      { role: 'assistant', content: 'I answered NO_REPLY to the ping on purpose.', timestamp: 201 }
    ])

    expect(result.map(m => m.text)).toContain('I answered NO_REPLY to the ping on purpose.')
    expect(result[0].role).toBe('system')
  })

  it('drops a marker that closes a wake reply on its own line', () => {
    const result = painted([
      processRow(100),
      { role: 'assistant', content: 'Build is green.\n\nNO_REPLY', timestamp: 101 }
    ])

    expect(result[1]).toEqual({ role: 'assistant', text: 'Build is green.' })
  })

  it('keeps a persons answer whole, its last line included', () => {
    const result = painted([
      { role: 'user', content: 'the literal?', timestamp: 100 },
      { role: 'assistant', content: 'The literal value is:\nNO_REPLY', timestamp: 101 }
    ])

    expect(result[1]).toEqual({ role: 'assistant', text: 'The literal value is:\nNO_REPLY' })
  })

  it('keeps public commentary a silent final spoke, and its wake', () => {
    const result = painted([
      processRow(200),
      {
        role: 'assistant',
        content: 'NO_REPLY',
        timestamp: 201,
        display_commentary: ['Build is ready at https://example.test/report.']
      }
    ])

    expect(result.map(m => m.role)).toEqual(['system', 'assistant'])
    expect(result[1].text).toBe('Build is ready at https://example.test/report.')
  })

  it('treats a marker kept only in codex message items as silence', () => {
    expect(
      painted([
        { role: 'user', content: 'hi', timestamp: 100 },
        processRow(200),
        {
          role: 'assistant',
          content: '',
          timestamp: 201,
          reasoning: 'nothing new',
          codex_message_items: [
            { type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'NO_REPLY' }] }
          ]
        }
      ])
    ).toEqual([{ role: 'user', text: 'hi' }])
  })

  it('keeps a background-process row whose turn has not been stored yet', () => {
    expect(painted([{ role: 'user', content: 'hi', timestamp: 100 }, processRow(200)]).map(m => m.role)).toEqual([
      'user',
      'system'
    ])
  })

  it('keeps a cron delivery the bot answered with silence: its output is for the reader', () => {
    const result = painted([
      {
        role: 'user',
        content: '[Cronjob "Team report" output]\n\nApex morning run is done.',
        display_content: 'Apex morning run is done.',
        display_kind: 'internal_notification',
        display_metadata: { cron_delivery: { job_name: 'Team report' }, display_text: 'Team report' },
        timestamp: 200
      },
      { role: 'assistant', content: 'NO_REPLY', timestamp: 201 }
    ])

    expect(result).toHaveLength(1)
    expect(result.some(m => m.text.includes('NO_REPLY'))).toBe(false)
  })

  it('keeps the error card of a failed turn whose boundary row reads like a marker', () => {
    const messages = toChatMessages([
      processRow(200),
      {
        role: 'assistant',
        content: 'NO_REPLY',
        timestamp: 201,
        display_kind: 'failed_turn',
        display_metadata: {
          error: 'provider 500',
          error_surface: { code: 'provider_error', layer: 'provider', retryable: true }
        }
      }
    ] as never)

    expect(messages.some(message => message.error)).toBe(true)
    expect(messages[0].role).toBe('system')
  })

  it('keeps the wake of a turn that spoke through codex message items', () => {
    const result = painted([
      processRow(200),
      {
        role: 'assistant',
        content: '',
        timestamp: 201,
        codex_message_items: [
          { type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'Build finished: 3 failures' }] }
        ]
      },
      { role: 'assistant', content: 'NO_REPLY', timestamp: 202 }
    ])

    expect(result.map(m => m.role)).toEqual(['system', 'assistant'])
    expect(result[1].text).toBe('Build finished: 3 failures')
  })
})

// The older-page offset is counted in backend rows (store/transcript-tail.ts), so the messages a
// page hydrates into must account for every row it held, hidden ones included, or "Show earlier"
// skips real rows after a retention release.
describe('serverRowSpan with silent turns', () => {
  const span = (rows: unknown[]) =>
    toChatMessages(rows as never).reduce((sum, message) => sum + (message.serverRowSpan ?? 1), 0)

  it('counts a hidden silent wake between two real turns', () => {
    const rows = [
      { role: 'user', content: 'hi', timestamp: 1 },
      { role: 'assistant', content: 'hello', timestamp: 2 },
      processRow(3),
      { role: 'assistant', content: 'NO_REPLY', timestamp: 4 },
      { role: 'user', content: 'next', timestamp: 5 },
      { role: 'assistant', content: 'ok', timestamp: 6 }
    ]

    expect(toChatMessages(rows as never)).toHaveLength(4)
    expect(span(rows)).toBe(rows.length)
  })

  it('counts a silent wake at the start and at the end of a page', () => {
    const rows = [
      processRow(1),
      { role: 'assistant', content: 'NO_REPLY', timestamp: 2 },
      { role: 'user', content: 'next', timestamp: 3 },
      { role: 'assistant', content: 'ok', timestamp: 4 },
      processRow(5),
      { role: 'assistant', content: '[SILENT]', timestamp: 6, reasoning: 'nothing to add' }
    ]

    expect(toChatMessages(rows as never)).toHaveLength(2)
    expect(span(rows)).toBe(rows.length)
  })
})
