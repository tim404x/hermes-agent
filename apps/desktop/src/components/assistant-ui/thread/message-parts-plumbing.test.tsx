// Desktop polish batch 1 (P4): tool plumbing is never painted, and connector rows follow the
// same show-tool-activity switch as every other tool row. Before, a labelled call (connector,
// MCP, the tool_search/tool_describe bridge) rendered ahead of the switch, so "Reading tool
// details · 3 tools" and "Airtable · get page layout" x2 sat under replies with tool activity off.
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { toChatMessages } from '@/lib/chat-messages'
import { toRuntimeMessage } from '@/lib/chat-runtime'
import { $showToolActivity } from '@/store/tool-activity'
import type { SessionMessage } from '@/types/hermes'

import { stubThreadEnvironment, ThreadRuntime } from '../test-utils'

import { Thread } from '.'

stubThreadEnvironment()

const label = (app: string, text: string, name: string, kind = 'mcp') => [
  { action: '', app, emoji: '', kind, name, preview: '', text }
]

/** One stored turn: the user's prompt, an assistant row calling `name`, its result, the reply. */
function turn(name: string, args: Record<string, unknown>, labels?: unknown): SessionMessage[] {
  const id = `call-${name}`

  return [
    { role: 'user', content: 'check the heatmap', timestamp: 1 },
    {
      role: 'assistant',
      content: '',
      timestamp: 2,
      tool_calls: [{ id, type: 'function', function: { name, arguments: JSON.stringify(args) } }],
      ...(labels ? { tool_call_labels: { [id]: labels } } : {})
    },
    { role: 'tool', content: JSON.stringify({ ok: true }), tool_call_id: id, tool_name: name, timestamp: 3 },
    { role: 'assistant', content: 'Both heatmap pages are fixed.', timestamp: 4 }
  ] as SessionMessage[]
}

function threadText(rows: SessionMessage[]): string {
  const { container, unmount } = render(
    <ThreadRuntime messages={toChatMessages(rows).map(toRuntimeMessage)}>
      <Thread />
    </ThreadRuntime>
  )

  const text = container.textContent ?? ''
  unmount()

  return text
}

const describeTurn = turn(
  'tool_describe',
  { names: ['mcp__airtable__get_page_layout'] },
  label('Reading tool details', 'Reading tool details · 3 tools', 'tool_describe', 'tool')
)

const connectorTurn = turn(
  'tool_call',
  { calls: [{ arguments: { pageId: 'pag1' }, name: 'mcp__airtable__get_page_layout' }] },
  label('Airtable', 'Airtable · get page layout', 'mcp__airtable__get_page_layout')
)

afterEach(() => {
  cleanup()
  $showToolActivity.set(true)
})

describe('tool plumbing and connector rows', () => {
  it.each([true, false])('never paints tool discovery or a process poll (tool activity %s)', shown => {
    $showToolActivity.set(shown)

    for (const rows of [
      describeTurn,
      turn('tool_search', { query: 'airtable layout' }),
      turn('skill_view', { name: 'notion' }),
      turn('process_manage', { action: 'wait', session_id: 'proc_1', timeout: 300 })
    ]) {
      const text = threadText(rows)
      expect(text).toContain('Both heatmap pages are fixed.')
      expect(text).not.toMatch(/tool details|Searching tools|notion|proc_1|Process Manage/i)
    }
  })

  it('a connector call follows the show-tool-activity switch', () => {
    $showToolActivity.set(false)
    expect(threadText(connectorTurn)).not.toMatch(/page layout/i)

    $showToolActivity.set(true)
    expect(threadText(connectorTurn)).toMatch(/Airtable/)
  })
})
