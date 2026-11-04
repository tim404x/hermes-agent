import { describe, expect, it } from 'vitest'

import { withCoveredLiveToolResults } from './coverage'
import type { ChatMessage, ChatMessagePart } from './types'

const call = (toolCallId: string, done?: { at: number; result: unknown }): ChatMessagePart =>
  ({
    type: 'tool-call',
    toolCallId,
    toolName: 'process_manage',
    args: {},
    argsText: '{}',
    timestamp: 1,
    ...(done ? { completedAt: done.at, result: done.result, isError: false } : {})
  }) as ChatMessagePart

const assistant = (id: string, parts: ChatMessagePart[], pending = false): ChatMessage => ({
  id,
  role: 'assistant',
  parts,
  ...(pending && { pending })
})

const resultOf = (messages: ChatMessage[], toolCallId: string) =>
  messages
    .flatMap(message => message.parts)
    .find(part => part.type === 'tool-call' && part.toolCallId === toolCallId) as
    (ChatMessagePart & { completedAt?: number; result?: unknown }) | undefined

describe('withCoveredLiveToolResults', () => {
  it('a stored call without its row yet takes the result the live stream delivered', () => {
    const stored = [assistant('stored', [call('a', { at: 2, result: 'a done' }), call('b')])]

    const live = [
      assistant('live', [call('a', { at: 2, result: 'a done' }), call('b', { at: 3, result: 'b done' })], true)
    ]

    const next = withCoveredLiveToolResults(stored, live)

    expect(resultOf(next, 'b')?.result).toBe('b done')
    expect(resultOf(next, 'b')?.completedAt).toBe(3)
    // The stored row keeps its own identity: only the missing result is filled.
    expect(next[0].id).toBe('stored')
  })

  it('a stored result always wins over the live copy', () => {
    const stored = [assistant('stored', [call('a', { at: 2, result: 'from the row' })])]
    const live = [assistant('live', [call('a', { at: 9, result: 'from the event' })], true)]

    expect(withCoveredLiveToolResults(stored, live)).toBe(stored)
  })

  it('takes nothing without an ordered tool occurrence anchoring the coverage', () => {
    const stored = [assistant('stored', [call('a')])]
    // Same id, but not the covered prefix: the live interval starts elsewhere.
    const live = [assistant('live', [call('x', { at: 2, result: 'x' }), call('a', { at: 3, result: 'other a' })], true)]

    expect(withCoveredLiveToolResults(stored, live)).toBe(stored)
  })
})
