// Desktop polish batch 1 (Thu 19 Nov 2026, plan ~/.hermes/plans/desktop-polish-1119.md): the
// thread reads like a conversation. These are REAL stored rows from that day (REST page as the
// desktop loads it, trimmed), rendered through the real hydration and the real Thread:
// - nothing a machine wrote paints as the user's own bubble (cron output, a bot's task brief,
//   background-process completions, inter-agent deliveries);
// - no shell command, path, process id, harness frame, interrupt sentinel or tool plumbing leaks
//   into the visible text;
// - the user's real rows (prompts and mid-turn steers) are the only bubbles.
// Old history is projected at render time; nothing here is rewritten in a DB.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { toChatMessages } from '@/lib/chat-messages'
import fixture from '@/lib/chat-messages/__fixtures__/polish-1119-real-rows.json'
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

type Slice = keyof Omit<typeof fixture, '_doc'>

const rows = (slice: Slice) => fixture[slice] as unknown as SessionMessage[]

// The backend projection (`bot_brief`) the new gateway serves for the first unattributed row of a
// side window a bot seeded; the raw row on disk is an untyped `user` row.
function projectedTaskTab(): SessionMessage[] {
  return rows('neo_task_tab').map((row, index) =>
    index === 0
      ? ({
          ...row,
          display_kind: 'bot_brief',
          display_metadata: { author: { id: 'bot:default', name: 'Neo | Chief of Staff' } }
        } as unknown as SessionMessage)
      : row
  )
}

function renderThread(messages: SessionMessage[]) {
  const runtimeMessages = toChatMessages(messages).map(toRuntimeMessage)

  const view = render(
    <ThreadRuntime messages={runtimeMessages}>
      <Thread />
    </ThreadRuntime>
  )

  // The transcript windows to the latest turns; page in every earlier row so all of them render.
  for (let page = 0; page < 50; page++) {
    const earlier = view.queryByRole('button', { name: 'Show earlier messages' })

    if (!earlier) {
      break
    }

    fireEvent.click(earlier)
  }

  return view
}

const bubbleTexts = (container: HTMLElement) =>
  Array.from(container.querySelectorAll('.composer-human-message')).map(node => (node.textContent ?? '').trim())

// Machine-made text that must never be visible in the thread.
const LEAKS = ['NO_REPLY', '[Cronjob', '[IMPORTANT', '/home/', 'proc_', '$(', 'Operation interrupted', 'tool details', '[OUT-OF-BAND']

function expectNoLeaks(text: string) {
  for (const leak of LEAKS) {
    expect(text, `visible thread text leaks "${leak}"`).not.toContain(leak)
  }
}

describe('polish 1119: real rows read like a conversation', { timeout: 60_000 }, () => {
  it('Kepler main chat: only the user prompts and steers are bubbles; no frame, sentinel or plumbing', () => {
    $showToolActivity.set(false)
    const { container } = renderThread(rows('kepler_main'))
    const text = container.textContent ?? ''

    expectNoLeaks(text)
    expect(text).not.toMatch(/page layout/i)

    const bubbles = bubbleTexts(container)
    expect(bubbles.length).toBeGreaterThan(5)
    expect(bubbles).toContain('ok whats next? what abt emulate?')
    expect(bubbles).toContain('did i receive a nda from them?')
    expect(bubbles.some(bubble => bubble.startsWith('Message from'))).toBe(false)
    // The heatmap build's completion woke a turn that chose silence (NO_REPLY): neither the wake
    // nor the marker paints (silence carry), so the chat ends on Kepler's real update.
    expect(text).not.toContain('Deal heatmap finished')
  })

  it('tool plumbing is never painted, even with tool activity shown; connector rows follow the switch', () => {
    // Kepler's reply to Neo's note folds into a "Replied to Neo" event; open it to see its tool rows.
    const openReply = () => fireEvent.click(screen.getByRole('button', { name: /Replied to Neo/ }))

    $showToolActivity.set(true)
    const shown = renderThread(rows('kepler_main'))
    openReply()
    expect(shown.container.textContent).not.toContain('Reading tool details')
    shown.unmount()

    $showToolActivity.set(false)
    const hidden = renderThread(rows('kepler_main'))
    openReply()
    expect(hidden.container.textContent).not.toMatch(/page layout/i)
    expect(hidden.container.textContent).toContain('The heatmap is ready for you to publish')
  })

  it("Forge's morning cron output is an event row, never the user's bubble", () => {
    $showToolActivity.set(false)
    const { container } = renderThread(rows('forge_cron'))
    const text = container.textContent ?? ''

    expectNoLeaks(text)
    expect(bubbleTexts(container)).toEqual([])
    expect(text).toContain('Team report')
    expect(text).not.toContain('scheduled job, not the user')
  })

  it("Neo's task-tab brief is a quiet event, not the user's message", () => {
    $showToolActivity.set(false)
    const { container } = renderThread(projectedTaskTab())
    const text = container.textContent ?? ''

    expectNoLeaks(text)
    expect(bubbleTexts(container)).toEqual([])
    expect(text).toContain('Brief from Neo')
    expect(text).not.toContain('Chief of Staff')
    expect(text).not.toContain('This tab IS that tab')
    expect(text).toContain('Asim handoff')
  })

  it("Neo's background job reads as its work, not its shell command", () => {
    $showToolActivity.set(false)
    const { container } = renderThread(rows('neo_bg_process'))
    const text = container.textContent ?? ''

    expectNoLeaks(text)
    expect(text).toContain('Asim short finished')
    expect(text).not.toContain('Background Process Finished')
  })
})
