import type { GatewayEvent } from '@hermes/shared'
import { act, cleanup } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { toChatMessages } from '@/lib/chat-messages'
import type { SessionMessage } from '@/types/hermes'

import { renderMessageStream } from './test-harness'

// Ack first: a long turn opens with the model's own one-line ack (text beside its first tool call,
// delivered as `message.interim`). It must read first, ahead of the activity, live and after reload,
// and the answer must come after the activity.

const SID = 'ack-first-order'
const ACK = 'On it: pulling the October hours from the sheet, about a minute.'
const FINAL = 'October closed at 41,200 hours, 6% over target.'

afterEach(cleanup)

type Part = ReturnType<typeof toChatMessages>[number]['parts'][number]

const flat = (messages: ReturnType<typeof toChatMessages>) =>
  messages.flatMap((message, index) =>
    message.parts.flatMap((part: Part) =>
      part.type === 'text' && part.text.trim()
        ? [{ index, kind: 'text', text: part.text.trim() }]
        : part.type === 'tool-call'
          ? [{ index, kind: 'tool', text: '' }]
          : []
    )
  )

it('paints the ack ahead of the activity and the answer after it, live and after reload', async () => {
  const stream = renderMessageStream(SID)

  const emit = async (type: GatewayEvent['type'], payload: GatewayEvent['payload']) => {
    await act(() => stream.handleEvent({ type, payload, session_id: SID }))
  }

  await emit('message.start', {})
  await emit('message.interim', { text: ACK, already_streamed: false, timestamp: 1 })

  // While the tools run, the ack is already its own sealed message.
  const sealed = stream.state().messages.filter(message => message.role === 'assistant')
  expect(sealed.map(message => message.parts.filter(part => part.type === 'text').length)).toEqual([1])
  expect(sealed[0].interim).toBe(true)

  await emit('tool.start', { tool_id: 'call-1', name: 'terminal', args: { command: 'ls' }, timestamp: 2 })
  await emit('tool.complete', { tool_id: 'call-1', name: 'terminal', result: 'ok', timestamp: 3 })
  await emit('message.delta', { text: FINAL, timestamp: 4 })
  await emit('message.complete', { text: FINAL, timestamp: 5 })

  const live = flat(stream.state().messages)

  const rows: SessionMessage[] = [
    { id: 1, role: 'user', content: 'How did October close?', timestamp: 0 },
    {
      id: 2,
      role: 'assistant',
      content: ACK,
      timestamp: 1,
      tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'terminal', arguments: '{"command":"ls"}' } }]
    },
    { id: 3, role: 'tool', content: 'ok', tool_call_id: 'call-1', tool_name: 'terminal', timestamp: 3 },
    { id: 4, role: 'assistant', content: FINAL, timestamp: 4 }
  ]

  const restored = flat(toChatMessages(rows)).filter(item => item.text !== 'How did October close?')

  for (const order of [live, restored]) {
    expect(order.map(item => item.kind)).toEqual(['text', 'tool', 'text'])
    expect(order[0].text).toBe(ACK)
    expect(order[2].text).toBe(FINAL)
  }

  // Live, the ack stays a message of its own; the answer is never folded into it.
  expect(live[0].index).toBeLessThan(live[2].index)
})
