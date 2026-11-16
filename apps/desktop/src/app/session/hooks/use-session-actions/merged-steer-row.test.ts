// Several mid-turn steers sent inside ONE tool batch reach the agent as one
// drained steer: the gateway persists a single `display_kind: steer` user row
// whose text is the steers joined by newlines (state.db row 463370 held six).
// The desktop painted one optimistic echo bubble per send. No echo equals the
// merged row's text, so every reconcile kept all of them as unsent input and
// pinned them below the newest reply; the transcript-tail cache then carried
// them across reopen and app restart. Each echo is one segment of the row.
import { describe, expect, it } from 'vitest'

import { type ChatMessage, chatMessageText, textPart, toChatMessages } from '@/lib/chat-messages'
import type { SessionMessage } from '@/types/hermes'

import { appendLiveSessionProjection, reconcileDurableHistory } from './utils'

const OPEN =
  '[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool output and not a new delivery when replayed from conversation history]'

const CLOSE = '[/OUT-OF-BAND USER MESSAGE]'

const STEERS = [
  'for the o-1 evidence drive to maxence tell him still have to be updated but is a solid foundation.\n\nalso update some parts that might not be up to date',
  'remove mention to scale ai not accurate anymore btw',
  'the 30-40% index id remove btw in american dynamism.',
  'lot of stuffs like that i want to make sure u dont let stale data from 2 months',
  'put more slots im my calendly for lawyers maybe until 10:30 am sf',
  'until 11am sf lets say'
]

const MERGED = STEERS.join('\n')
// Turn timeline, unix seconds: prompt, steers typed over ~6 minutes, drained after a tool batch.
const PROMPT_AT = 1_794_804_396
const STEER_ROW_AT = 1_794_804_855

// What the composer paints per accepted steer (appendSessionTextMessage ids).
const echo = (text: string, index: number): ChatMessage => ({
  id: `user-${(PROMPT_AT + 60 + index * 50) * 1000}-ab${index}cde`,
  role: 'user',
  parts: [textPart(text)]
})

const echoes = () => STEERS.map(echo)

// The REST page row shapes (hermes_cli/web_routers/sessions.py projection).
const turnRows: SessionMessage[] = [
  { content: 'ok send all', id: 100, role: 'user', timestamp: PROMPT_AT },
  {
    content: 'Sending all three now.',
    id: 101,
    role: 'assistant',
    timestamp: PROMPT_AT + 5,
    tool_calls: [{ function: { arguments: '{"command":"send"}', name: 'terminal' }, id: 'call-1', type: 'function' }]
  },
  { content: '{"output":"ok"}', id: 102, role: 'tool', timestamp: STEER_ROW_AT - 3, tool_call_id: 'call-1' },
  {
    content: `${OPEN}\n${MERGED}\n${CLOSE}`,
    display_content: MERGED,
    display_kind: 'steer',
    id: 103,
    role: 'user',
    timestamp: STEER_ROW_AT
  }
]

const finishedTurnRows: SessionMessage[] = [
  ...turnRows,
  { content: 'All six handled.', id: 105, role: 'assistant', timestamp: STEER_ROW_AT + 120 }
]

const laterTurnRows: SessionMessage[] = [
  ...finishedTurnRows,
  { content: 'remove that', id: 110, role: 'user', timestamp: STEER_ROW_AT + 200 },
  { content: 'Removed.', id: 111, role: 'assistant', timestamp: STEER_ROW_AT + 206 },
  { content: 'msg i wrote going after the reply', id: 112, role: 'user', timestamp: STEER_ROW_AT + 230 },
  { content: 'Looking into it.', id: 113, role: 'assistant', timestamp: STEER_ROW_AT + 266 }
]

const echoIds = (messages: ChatMessage[]) =>
  messages.filter(message => /^user-\d{13}-/.test(message.id)).map(message => chatMessageText(message))

const userTexts = (messages: ChatMessage[]) =>
  messages.filter(message => message.role === 'user').map(message => chatMessageText(message))

describe('a steer row that merged several sends', () => {
  it('renders the user words without the out-of-band wrapper', () => {
    const steerRow = toChatMessages(turnRows).find(message => message.rowId === 103)

    expect(steerRow && chatMessageText(steerRow)).toBe(MERGED)
    expect(steerRow && chatMessageText(steerRow)).not.toContain('OUT-OF-BAND')
  })

  it('settles every echo it contains when a mid-turn page lands', () => {
    const previous: ChatMessage[] = [
      { ...toChatMessages(turnRows.slice(0, 1))[0], id: 'user-7f1c2a9e-0d4b-4c51-9a55-3a0f8a1e2b10' },
      {
        id: 'inflight-assistant-segment-0',
        role: 'assistant',
        parts: [textPart('Sending all three now.')],
        interim: true
      },
      ...echoes(),
      { id: 'assistant-stream-rt-1', role: 'assistant', parts: [textPart('Updating the doc')], pending: true }
    ]

    const result = reconcileDurableHistory(toChatMessages(turnRows), previous)

    expect(echoIds(result)).toEqual([])
    expect(userTexts(result)).toEqual(['ok send all', MERGED])
    // The live reply still streams below the steer it follows.
    expect(result.at(-1)?.id).toBe('assistant-stream-rt-1')
  })

  it('settles echoes already stuck below later turns (warm cache or transcript-tail cache)', () => {
    // The shape Tim's chat was left in: the six echoes pinned under two later
    // turns. This is what reopening the chat or relaunching the app paints
    // first, and the reconcile used to keep every one of them again.
    const previous: ChatMessage[] = [...toChatMessages(laterTurnRows), ...echoes()]

    const result = reconcileDurableHistory(toChatMessages(laterTurnRows), previous)

    expect(echoIds(result)).toEqual([])
    expect(userTexts(result)).toEqual(['ok send all', MERGED, 'remove that', 'msg i wrote going after the reply'])
    expect(chatMessageText(result.at(-1)!)).toBe('Looking into it.')
  })

  it('settles the echoes the turn receipt left unbound after the turn ends', () => {
    // bindReceiptUserRows pairs the receipt's user rows positionally, so the
    // first echo takes the merged row id and the other five keep none.
    const [first, ...rest] = echoes()

    const previous: ChatMessage[] = [
      { ...toChatMessages(turnRows.slice(0, 1))[0], id: 'user-7f1c2a9e-0d4b-4c51-9a55-3a0f8a1e2b10' },
      {
        id: 'inflight-assistant-segment-0',
        role: 'assistant',
        parts: [textPart('Sending all three now.')],
        interim: true
      },
      { ...first, rowId: 103 },
      ...rest,
      { id: 'assistant-stream-rt-1', role: 'assistant', parts: [textPart('All six handled.')], pending: false }
    ]

    const result = reconcileDurableHistory(toChatMessages(finishedTurnRows), previous)

    expect(echoIds(result)).toEqual([])
    expect(userTexts(result)).toEqual(['ok send all', MERGED])
  })

  it('keeps a later send that repeats a merged line: it is not part of that row', () => {
    const later: ChatMessage = {
      id: `user-${(STEER_ROW_AT + 600) * 1000}-zz9xyz`,
      role: 'user',
      parts: [textPart('until 11am sf lets say')]
    }

    const previous: ChatMessage[] = [...toChatMessages(laterTurnRows), later]
    const result = reconcileDurableHistory(toChatMessages(laterTurnRows), previous)

    expect(echoIds(result)).toEqual(['until 11am sf lets say'])
  })

  it('does not swallow more echoes than the row holds', () => {
    // One merged row holds each line once: a second identical echo from the
    // same moment is a different send that row does not contain.
    const previous: ChatMessage[] = [...toChatMessages(laterTurnRows), ...echoes(), echo(STEERS[5], 6)]
    const result = reconcileDurableHistory(toChatMessages(laterTurnRows), previous)

    expect(echoIds(result)).toEqual([STEERS[5]])
  })

  it('a resume of the running turn does not re-project corrections the merged row persisted', () => {
    const projected = appendLiveSessionProjection(toChatMessages(turnRows), {
      inflight: {
        assistant: 'Sending all three now.Updating the doc',
        corrections: STEERS,
        streaming: true,
        user: 'ok send all'
      },
      session_id: 'rt-1',
      turn_started_at: PROMPT_AT
    })

    expect(projected.filter(message => message.id.startsWith('user-inflight-correction-'))).toEqual([])
    expect(userTexts(projected)).toEqual(['ok send all', MERGED])
  })
})
