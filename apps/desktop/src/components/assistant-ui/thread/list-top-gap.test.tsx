import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type * as WindowsStore from '@/store/windows'

import { stubThreadEnvironment, stubThreadViewportSize } from '../test-utils'

import { TranscriptWindowProvider } from './transcript-window'

import { Thread } from '.'

const windowKind = vi.hoisted(() => ({ secondary: false }))

vi.mock('@/store/windows', async importOriginal => ({
  ...(await importOriginal<typeof WindowsStore>()),
  isSecondaryWindow: () => windowKind.secondary
}))

stubThreadEnvironment()
stubThreadViewportSize()

afterEach(() => {
  windowKind.secondary = false
})

const createdAt = new Date('2026-11-19T00:00:00.000Z')

const userRow = (id: string): ThreadMessage =>
  ({
    id,
    role: 'user',
    content: [{ type: 'text', text: 'hello' }],
    attachments: [],
    createdAt,
    metadata: { custom: {} }
  }) as ThreadMessage

const assistantRow = (id: string): ThreadMessage =>
  ({
    id,
    role: 'assistant',
    content: [{ type: 'text', text: '**Handoff** where it stands' }],
    status: { type: 'complete', reason: 'stop' },
    createdAt,
    metadata: { unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [], custom: {} }
  }) as ThreadMessage

function Harness({ messages }: { messages: ThreadMessage[] }) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({ isRunning: false, messages, onNew: async () => {} })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <TranscriptWindowProvider value={{ olderAvailable: false, expandWindow: () => {} }}>
        <Thread sessionKey="top-gap" />
      </TranscriptWindowProvider>
    </AssistantRuntimeProvider>
  )
}

function contentTopPad(messages: ThreadMessage[]): string {
  const { container, unmount } = render(<Harness messages={messages} />)
  const content = container.querySelector<HTMLElement>('[data-slot="aui_thread-content"]')

  expect(content).toBeTruthy()

  const pads = [...content!.classList].filter(cls => cls.startsWith('pt-'))
  unmount()

  expect(pads).toHaveLength(1)

  return pads[0]
}

// Inside the layout tree --titlebar-height is 0 and the tab strip sits outside
// the scroller, so a titlebar-relative pad alone resolved to 0 and a chat that
// opens on an assistant message (a Bot Mode DM whose user row is not painted)
// touched the tabs. The air above the first row is the container's job, floored
// at one shared token, whatever kind of row comes first.
describe('thread content top gap', () => {
  it('floors the top pad at --thread-top-gap in the main window, whatever row opens the chat', () => {
    const assistantFirst = contentTopPad([assistantRow('a-1')])
    const userFirst = contentTopPad([userRow('u-1'), assistantRow('a-1')])

    expect(assistantFirst).toContain('var(--thread-top-gap)')
    expect(assistantFirst).toMatch(/^pt-\[max\(/)
    expect(userFirst).toBe(assistantFirst)
  })

  it('floors the top pad at --thread-top-gap in secondary windows too', () => {
    windowKind.secondary = true

    const pad = contentTopPad([assistantRow('a-1')])

    expect(pad).toContain('var(--thread-top-gap)')
    expect(pad).toMatch(/^pt-\[max\(/)
  })
})
