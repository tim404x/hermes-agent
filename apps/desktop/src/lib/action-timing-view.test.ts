import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $messages, $selectedStoredSessionId } from '@/store/session'
import { $sessionStates } from '@/store/session-states'

import { __flushActionTimingForTests, __resetActionTimingForTests, ACTION_TIMING_LINE_PREFIX } from './action-timing'
import { __resetViewTimingForTests, viewActionAbandon, viewActionBind, viewActionStart } from './action-timing-view'

let now = 0

const msg = (id: string) => ({ id, role: 'assistant', parts: [{ type: 'text', text: id }] }) as unknown as ChatMessage

function flushed(): { a: Record<string, number[]>; drop?: Record<string, number> } | null {
  const line = __flushActionTimingForTests()

  return line ? JSON.parse(line.slice(ACTION_TIMING_LINE_PREFIX.length)) : null
}

describe('action-timing-view (switch / open_chat painted)', () => {
  let frames: FrameRequestCallback[]

  beforeEach(() => {
    __resetActionTimingForTests()
    __resetViewTimingForTests()
    now = 100
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    frames = []
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      frames.push(cb)

      return frames.length
    })
    $messages.set([msg('old')])
    $selectedStoredSessionId.set('previous')
    $sessionStates.set({})
  })

  afterEach(() => {
    __resetViewTimingForTests()
    __resetActionTimingForTests()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  const runFrames = () => {
    const pending = frames
    frames = []

    for (const cb of pending) {
      cb(now)
    }
  }

  it('open_chat ends on the frame after the target transcript lands in the main pane', () => {
    viewActionStart('open_chat', 'chat-1', 'chat-1')
    now += 50
    // Selection flips first while the old transcript is still on screen: not painted yet.
    $selectedStoredSessionId.set('chat-1')
    expect(frames).toHaveLength(0)
    now += 100
    $messages.set([msg('a'), msg('b')])
    expect(frames).toHaveLength(1)
    now += 16
    runFrames()
    expect(flushed()?.a).toEqual({ open_chat: [166] })
  })

  it('switch binds late and ends when the bot chat tile state is published', () => {
    viewActionStart('switch', 'local::atlas')
    now += 300
    viewActionBind('switch', 'local::atlas', 'atlas-chat')
    now += 200
    $sessionStates.set({ rt1: createClientSessionState('atlas-chat', [msg('x')]) })
    runFrames()
    expect(flushed()?.a).toEqual({ switch: [500] })
  })

  it('a fronted open tab is painted on the next frame', () => {
    viewActionStart('switch', 'local::forge')
    now += 5
    viewActionBind('switch', 'local::forge', 'forge-chat', { painted: true })
    now += 16
    runFrames()
    expect(flushed()?.a).toEqual({ switch: [21] })
  })

  it('a newer view replaces the older one; abandon is silent', () => {
    viewActionStart('switch', 'local::atlas')
    viewActionBind('switch', 'local::atlas', 'atlas-chat')
    viewActionStart('switch', 'local::kepler')
    // The superseded target landing no longer ends anything.
    $sessionStates.set({ rt1: createClientSessionState('atlas-chat', [msg('x')]) })
    runFrames()
    viewActionAbandon('switch', 'local::kepler')
    expect(flushed()).toBeNull()
  })

  it('re-picking the chat already on screen records nothing', () => {
    $selectedStoredSessionId.set('chat-1')
    viewActionStart('open_chat', 'chat-1', 'chat-1')
    runFrames()
    expect(flushed()).toBeNull()
  })

  it('unsubscribes from the stores once nothing is pending', () => {
    const listenersBefore = $messages.lc
    viewActionStart('open_chat', 'c', 'c')
    expect($messages.lc).toBe(listenersBefore + 1)
    $selectedStoredSessionId.set('c')
    $messages.set([msg('z')])
    runFrames()
    expect(flushed()?.a).toEqual({ open_chat: [0] })
    // Further store traffic costs nothing and records nothing.
    expect($messages.lc).toBe(listenersBefore)
    $messages.set([msg('z'), msg('y')])
    expect(frames).toHaveLength(0)
  })
})
