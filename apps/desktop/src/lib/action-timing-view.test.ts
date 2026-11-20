import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $messages, $selectedStoredSessionId } from '@/store/session'
import { $focusedStoredSessionId } from '@/store/session-focus'
import { $sessionStates } from '@/store/session-states'

// The fronted pane (in Bots mode: the workspace's active bot tile) is derived from the layout tree;
// a plain atom stands in for it here.
vi.mock('@/store/session-focus', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  $focusedStoredSessionId: atom<null | string>(null)
}))

const setFocused = (id: null | string) => ($focusedStoredSessionId as unknown as ReturnType<typeof atom<null | string>>).set(id)

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
    setFocused('previous')
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

  it('leaving a streaming chat: its deltas before and at the flip do not end the open', () => {
    viewActionStart('switch', 'local::delta')
    viewActionBind('switch', 'local::delta', 'delta-chat')
    now += 300
    $messages.set([msg('old'), msg('streaming')]) // the chat being left keeps streaming
    now += 400
    $selectedStoredSessionId.set('delta-chat') // the open path flips the selection first
    expect(frames).toHaveLength(0)
    now += 300
    $messages.set([]) // then drops the old transcript (loader)
    expect(frames).toHaveLength(0)
    now += 600
    $messages.set([msg('d1')]) // the target's transcript
    runFrames()
    expect(flushed()?.a).toEqual({ switch: [1600] })
  })

  it('switch binds late and ends when the bot chat tile is fronted with its transcript', () => {
    viewActionStart('switch', 'local::atlas')
    now += 300
    viewActionBind('switch', 'local::atlas', 'atlas-chat')
    now += 200
    $sessionStates.set({ rt1: createClientSessionState('atlas-chat', [msg('x')]) })
    // Loaded but not on screen yet: the cache ingests every session's events.
    expect(frames).toHaveLength(0)
    now += 40
    setFocused('atlas-chat')
    runFrames()
    expect(flushed()?.a).toEqual({ switch: [540] })
  })

  it("a busy bot's background deltas do not end the switch before its tile is on screen", () => {
    $sessionStates.set({ rt9: createClientSessionState('kepler-chat', [msg('k1')]) })
    viewActionStart('switch', 'local::kepler')
    viewActionBind('switch', 'local::kepler', 'kepler-chat')
    now += 30
    $sessionStates.set({ rt9: createClientSessionState('kepler-chat', [msg('k1'), msg('k2')]) })
    $sessionStates.set({ rt9: createClientSessionState('kepler-chat', [msg('k1'), msg('k2'), msg('k3')]) })
    expect(frames).toHaveLength(0)
    now += 270
    setFocused('kepler-chat')
    runFrames()
    expect(flushed()?.a).toEqual({ switch: [300] })
  })

  it('re-picking the bot tile already on screen records nothing', () => {
    $sessionStates.set({ rt2: createClientSessionState('forge-chat', [msg('f')]) })
    setFocused('forge-chat')
    viewActionStart('switch', 'local::forge')
    // The real path: focusExistingBotTab fronts the (already fronted) tile and binds `painted`.
    viewActionBind('switch', 'local::forge', 'forge-chat', { painted: true })
    runFrames()
    expect(flushed()).toBeNull()
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
