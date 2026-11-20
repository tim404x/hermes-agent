import { beforeEach, describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import type { SessionInfo } from '@/types/hermes'

import { makeSessionInfo } from '../test/session-info'

import { $activeGatewayProfile } from './profile'
import {
  $cronSessions,
  $messagingSessions,
  $selectedStoredSessionId,
  $sessions,
  $unreadFinishedSessionIds,
  setSessions
} from './session'
import { clearAllSessionStates, noteSilentCompletion, publishSessionState } from './session-states'
import { $sessionSeenCounts, $unreadFinishedMarkers } from './session-unread'

const session = (over: Partial<SessionInfo>): SessionInfo => makeSessionInfo({ id: 's1', ...over })

const finishTurn = (silent: boolean) => {
  publishSessionState('rt1', { ...createClientSessionState('s1'), busy: true })

  if (silent) {
    noteSilentCompletion('rt1')
  }

  publishSessionState('rt1', { ...createClientSessionState('s1'), busy: false })
}

// A silent wake (a background process finished, the bot answered NO_REPLY) stores two rows and shows
// nothing. Neither the live busy->idle edge nor the stored count growing may paint it unread.
describe('unread after a silent turn', () => {
  beforeEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $cronSessions.set([])
    $messagingSessions.set([])
    $selectedStoredSessionId.set('another-chat')
    $activeGatewayProfile.set('default')
    $sessionSeenCounts.set({})
    $unreadFinishedMarkers.set({})
    $unreadFinishedSessionIds.set([])
    setSessions([session({ message_count: 4 })])
  })

  it('stays read through the live edge and the list refresh that shows its rows', () => {
    finishTurn(true)
    expect($unreadFinishedSessionIds.get()).toEqual([])

    // A refresh already in flight still reports the old count; the next one has the two silent rows.
    setSessions([session({ message_count: 4 })])
    setSessions([session({ message_count: 6 })])
    expect($unreadFinishedSessionIds.get()).toEqual([])

    // Later real activity is unread again.
    setSessions([session({ message_count: 8 })])
    expect($unreadFinishedSessionIds.get()).toEqual(['s1'])
  })

  it('still lights a real completion', () => {
    finishTurn(false)
    setSessions([session({ message_count: 6 })])
    expect($unreadFinishedSessionIds.get()).toEqual(['s1'])
  })

  it('does not let a silent note outlive its turn', () => {
    noteSilentCompletion('rt1')
    finishTurn(false)
    expect($unreadFinishedSessionIds.get()).toEqual(['s1'])
  })
})
