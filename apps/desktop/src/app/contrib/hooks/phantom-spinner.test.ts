import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $activeSessionId, $unreadFinishedSessionIds } from '@/store/session'
import { $sessionStates, $workingSessionIds, clearAllSessionStates, publishSessionState } from '@/store/session-states'

import { UNCONFIRMED_TURN_GRACE_MS } from './settle-orphaned-turn'
import { rehydrateLiveSessionStatuses, resetLiveRuntimeTracking } from './use-background-sync'

/**
 * Phantom spinner (Tim, 21 Nov 2026): a desktop tab kept a live
 * "55s · 1.2k tokens · Untangling…" row under a prompt whose turn the backend
 * finished 18 minutes earlier, and replies other processes (Telegram) wrote to
 * the same session never showed, because busy also gates the transcript
 * refresh. The authoritative `session.active_list` snapshot reported the
 * runtime idle on every poll, yet the renderer kept the turn alive forever.
 */
const NOW = 1_795_311_600_000

const user = (id: string, text: string): ChatMessage => ({
  id,
  role: 'user',
  parts: [{ type: 'text', text }],
  timestamp: (NOW - 120_000) / 1000
})

const pendingAssistant = (id: string, text = ''): ChatMessage => ({
  id,
  role: 'assistant',
  parts: text ? [{ type: 'text', text }] : [],
  timestamp: (NOW - 119_000) / 1000,
  pending: true
})

const idleSnapshot = { sessions: [{ id: 'rt', session_key: 'stored', status: 'idle' as const }] }

describe('rehydrateLiveSessionStatuses — a turn the backend reports idle always ends', () => {
  beforeEach(() => {
    // The open tab: an idle background runtime would have its transcript
    // released on publish, which is not what this pins.
    $activeSessionId.set('rt')
    $unreadFinishedSessionIds.set([])
    resetLiveRuntimeTracking()
  })

  afterEach(() => {
    $activeSessionId.set(null)
    clearAllSessionStates()
    resetLiveRuntimeTracking()
    $unreadFinishedSessionIds.set([])
  })

  it('settles an unconfirmed submit whose events never arrived, past the grace window', () => {
    publishSessionState('rt', {
      ...createClientSessionState('stored', [user('u1', 'are all of them in s3 now?'), pendingAssistant('a1')]),
      awaitingResponse: true,
      busy: true,
      sawAssistantPayload: false,
      streamId: 'a1',
      turnLive: false,
      turnStartedAt: NOW - 120_000
    })

    rehydrateLiveSessionStatuses(idleSnapshot, NOW)

    const state = $sessionStates.get().rt!
    expect(state.busy).toBe(false)
    expect(state.awaitingResponse).toBe(false)
    expect(state.turnStartedAt).toBeNull()
    expect(state.streamId).toBeNull()
    // The empty "waiting for first token" shell is gone: no tail row can mount.
    expect(state.messages.map(m => m.id)).toEqual(['u1'])
    expect($workingSessionIds.get()).not.toContain('stored')
  })

  it('keeps a fresh submit inside the accept grace window', () => {
    publishSessionState('rt', {
      ...createClientSessionState('stored', [user('u1', 'hi'), pendingAssistant('a1')]),
      awaitingResponse: true,
      busy: true,
      sawAssistantPayload: false,
      streamId: 'a1',
      turnLive: false,
      turnStartedAt: NOW - (UNCONFIRMED_TURN_GRACE_MS - 5_000)
    })

    rehydrateLiveSessionStatuses(idleSnapshot, NOW)

    const state = $sessionStates.get().rt!
    expect(state.busy).toBe(true)
    expect(state.messages.map(m => m.id)).toEqual(['u1', 'a1'])
  })

  it('clears the turn clock and pending bubble, not just busy, for a confirmed turn', () => {
    publishSessionState('rt', {
      ...createClientSessionState('stored', [user('u1', 'q'), pendingAssistant('a1', 'Yes, all 320 are in S3')]),
      awaitingResponse: false,
      busy: true,
      sawAssistantPayload: true,
      streamId: 'a1',
      turnLive: true,
      turnStartedAt: NOW - 60_000
    })

    rehydrateLiveSessionStatuses(idleSnapshot, NOW)

    const state = $sessionStates.get().rt!
    expect(state.busy).toBe(false)
    expect(state.turnLive).toBe(false)
    // A cleared busy with an armed clock + running tail still lit the row.
    expect(state.turnStartedAt).toBeNull()
    expect(state.messages.find(m => m.id === 'a1')?.pending).toBe(false)
    // A late message.complete settles onto the kept bubble, never a duplicate.
    expect(state.heartbeatSettledStreamId).toBe('a1')
  })

  it('leaves a turn alone while the backend reports it starting', () => {
    publishSessionState('rt', {
      ...createClientSessionState('stored', [user('u1', 'q'), pendingAssistant('a1')]),
      awaitingResponse: true,
      busy: true,
      turnStartedAt: NOW - 120_000
    })

    rehydrateLiveSessionStatuses({ sessions: [{ id: 'rt', session_key: 'stored', status: 'starting' }] }, NOW)

    expect($sessionStates.get().rt!.busy).toBe(true)
  })

  it('drops a stranded pending shell when the runtime is reaped', () => {
    rehydrateLiveSessionStatuses({ sessions: [{ id: 'rt', session_key: 'stored', status: 'working' }] }, NOW)
    publishSessionState('rt', {
      ...createClientSessionState('stored', [user('u1', 'q'), pendingAssistant('a1')]),
      busy: true,
      turnLive: true,
      turnStartedAt: NOW - 60_000
    })

    rehydrateLiveSessionStatuses({ sessions: [] }, NOW)

    const state = $sessionStates.get().rt!
    expect(state.busy).toBe(false)
    expect(state.turnStartedAt).toBeNull()
    expect(state.messages.map(m => m.id)).toEqual(['u1'])
  })
})
