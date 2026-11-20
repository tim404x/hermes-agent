// Polish fix 1119: the quiet thread never hides real content and never relabels the user.
// - a failed plumbing call (process poll exiting nonzero) still renders when the turn stops;
// - a person's words typed internal_notification (voice lines) stay the person's bubble;
// - a pasted cron frame stays the person's message even when the bot chooses silence;
// - a one-line system notice is readable in full, links included.
import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { toChatMessages } from '@/lib/chat-messages'
import { chatMessageText } from '@/lib/chat-messages/parts'
import { toRuntimeMessage } from '@/lib/chat-runtime'
import { $showToolActivity } from '@/store/tool-activity'
import type { SessionMessage } from '@/types/hermes'

import { stubThreadEnvironment, ThreadRuntime } from '../test-utils'

import { Thread } from '.'

stubThreadEnvironment()
afterEach(() => {
  cleanup()
  $showToolActivity.set(true)
})

function renderRows(rows: SessionMessage[]) {
  return render(
    <ThreadRuntime messages={toChatMessages(rows).map(toRuntimeMessage)}>
      <Thread />
    </ThreadRuntime>
  )
}

const bubbleTexts = (container: HTMLElement) =>
  Array.from(container.querySelectorAll('.composer-human-message')).map(node => (node.textContent ?? '').trim())

/** A turn whose only tool call is a process_manage poll returning `result`, then a stop. */
function stoppedPollTurn(result: Record<string, unknown>, extra: Partial<SessionMessage> = {}): SessionMessage[] {
  return [
    { role: 'user', content: 'Run the invoice import.', timestamp: 1 },
    {
      role: 'assistant',
      content: '',
      timestamp: 2,
      tool_calls: [
        {
          id: 'poll',
          type: 'function',
          function: { name: 'process_manage', arguments: JSON.stringify({ action: 'poll', session_id: 'proc_1' }) }
        }
      ]
    },
    {
      role: 'tool',
      content: JSON.stringify(result),
      tool_call_id: 'poll',
      tool_name: 'process_manage',
      timestamp: 3,
      ...extra
    },
    { role: 'assistant', content: '', display_metadata: { interrupted: true }, timestamp: 4 }
  ] as SessionMessage[]
}

describe('polish fix 1119', { timeout: 60_000 }, () => {
  it.each([false, true])('shows a failed process poll when the turn stops (tool activity %s)', shown => {
    $showToolActivity.set(shown)

    const view = renderRows(
      stoppedPollTurn({ status: 'exited', exit_code: 2, output: 'Invoice import failed: database permission denied.' })
    )

    expect(view.container.textContent).toContain('permission denied')
  })

  it('shows a plumbing call that returned an error', () => {
    $showToolActivity.set(false)

    const view = renderRows(stoppedPollTurn({ error: 'process registry unavailable: disk full' }))

    expect(view.container.textContent).toContain('disk full')
  })

  it('still hides a successful process poll', () => {
    $showToolActivity.set(true)

    const view = renderRows(stoppedPollTurn({ status: 'running', output: 'still importing rows' }))

    expect(view.container.textContent).not.toContain('still importing rows')
  })

  it('retains a human message quoting a cron frame even when the bot chooses silence', () => {
    const content = '[Cronjob "Team report" output - scheduled job]\n\nSave this report for tomorrow.'

    const rows: SessionMessage[] = [
      { role: 'user', content, timestamp: 1 },
      { role: 'assistant', content: 'NO_REPLY', timestamp: 2 }
    ] as SessionMessage[]

    expect(toChatMessages(rows).some(row => row.role === 'user' && chatMessageText(row) === content)).toBe(true)
    expect(renderRows(rows).container.textContent).toContain('Save this report for tomorrow.')
  })

  it("a voice line typed internal_notification is the user's own bubble", () => {
    const view = renderRows([
      {
        role: 'user',
        display_kind: 'internal_notification',
        content: "Hey, what's on my calendar tomorrow morning?",
        timestamp: 1
      },
      { role: 'assistant', content: 'You have a 9 AM with Dany.', timestamp: 2 }
    ] as SessionMessage[])

    expect(bubbleTexts(view.container)).toContain("Hey, what's on my calendar tomorrow morning?")
    expect(view.container.textContent).not.toContain('Notification')
  })

  it('a typed background-process completion still renders as a process note, never the raw frame', () => {
    const view = renderRows([
      {
        role: 'user',
        display_kind: 'internal_notification',
        content:
          '[IMPORTANT: Background process proc_151f1eb6add3 completed normally (exit code 0).\nCommand: W=/home/hermes/.hermes/work/asim-short-1119; ls]',
        timestamp: 1
      },
      { role: 'assistant', content: 'The brief is done.', timestamp: 2 }
    ] as SessionMessage[])

    const text = view.container.textContent ?? ''

    expect(bubbleTexts(view.container)).toEqual([])
    expect(text).not.toContain('[IMPORTANT')
    expect(text).not.toContain('Notification')
    expect(text).toContain('finished')
  })
})

describe('one-line system notices are readable in full', () => {
  function SystemHarness({ text }: { text: string }) {
    const message = {
      id: 'system-1',
      role: 'system',
      content: [{ type: 'text', text }],
      createdAt: new Date('2026-05-01T00:00:00.000Z'),
      metadata: { custom: {} }
    } as unknown as ThreadMessage

    const runtime = useExternalStoreRuntime<ThreadMessage>({
      messages: [message],
      isRunning: false,
      onNew: async () => {}
    })

    return (
      <AssistantRuntimeProvider runtime={runtime}>
        <Thread />
      </AssistantRuntimeProvider>
    )
  }

  it('wraps the whole notice instead of truncating it, and keeps its link clickable', () => {
    const text =
      'Switched to the fallback model openrouter/anthropic/claude-sonnet because the primary provider returned 529 overloaded; status at https://status.example.com/incidents/42 and raise the limit in Settings -> Safety -> Approval timeout.'

    const view = render(<SystemHarness text={text} />)
    const row = view.container.querySelector('[data-role="system"]')

    expect(row?.textContent).toContain(text)
    expect(row?.querySelector('a[href="https://status.example.com/incidents/42"]')).not.toBeNull()
    expect(row?.querySelector('.truncate')).toBeNull()
    // A link inside a (disabled) button is not clickable.
    expect(row?.querySelector('button a')).toBeNull()
  })
})
