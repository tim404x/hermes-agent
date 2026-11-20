import type { GatewayEventName } from '@hermes/shared'
import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, renderHook } from '@testing-library/react'
import { useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { playCompletionSound } from '@/lib/completion-sound'
import { dispatchNativeNotification } from '@/store/native-notifications'
import { $selectedStoredSessionId, $unreadFinishedSessionIds, setMessages } from '@/store/session'
import { clearAllSessionStates } from '@/store/session-states'

import { useSessionStateCache } from '../use-session-state-cache'

import { useMessageStream } from './index'

vi.mock('@/store/native-notifications', () => ({ dispatchNativeNotification: vi.fn() }))
vi.mock('@/lib/completion-sound', () => ({ playCompletionSound: vi.fn() }))

const SID = 'silent-runtime'
const STORED = 'silent-stored'
const noop = async () => undefined

// A background session (another chat is selected): exactly where a finished turn would toast, beep
// and light the sidebar's green dot.
function mount() {
  const hook = renderHook(() => {
    const busyRef = useRef(false)

    const cache = useSessionStateCache({
      activeSessionId: SID,
      selectedStoredSessionId: STORED,
      busyRef,
      setAwaitingResponse: () => undefined,
      setBusy: value => {
        busyRef.current = value
      },
      setMessages
    })

    const stream = useMessageStream({
      activeSessionIdRef: cache.activeSessionIdRef,
      sessionStateByRuntimeIdRef: cache.sessionStateByRuntimeIdRef,
      updateSessionState: cache.updateSessionState,
      queryClient: new QueryClient(),
      hydrateFromStoredSession: vi.fn(noop),
      refreshHermesConfig: noop,
      refreshSessions: noop
    })

    return { cache, stream }
  })

  act(() => hook.result.current.cache.ensureSessionState(SID, STORED))

  return (type: GatewayEventName, payload: Record<string, unknown> = {}) =>
    act(() => hook.result.current.stream.handleGatewayEvent({ type, payload, session_id: SID }))
}

// A wake turn that chose silence (`message.complete` with `silent: true`, tui_gateway/prompt_turn.py)
// shows nothing in the thread, so nothing may announce it either.
describe('a silent completion', () => {
  beforeEach(() => {
    clearAllSessionStates()
    $selectedStoredSessionId.set('another-chat')
    $unreadFinishedSessionIds.set([])
    vi.mocked(dispatchNativeNotification).mockClear()
    vi.mocked(playCompletionSound).mockClear()
  })

  afterEach(cleanup)

  it('raises no OS notification, no completion sound and no unread dot', () => {
    const send = mount()

    send('message.start')
    send('message.complete', { text: '', status: 'complete', silent: true })

    expect(dispatchNativeNotification).not.toHaveBeenCalled()
    expect(playCompletionSound).not.toHaveBeenCalled()
    expect($unreadFinishedSessionIds.get()).not.toContain(STORED)
  })

  it('leaves a real completion announced', () => {
    const send = mount()

    send('message.start')
    send('message.complete', { text: 'Build failed: 3 tests.', status: 'complete' })

    expect(dispatchNativeNotification).toHaveBeenCalledTimes(1)
    expect(playCompletionSound).toHaveBeenCalledTimes(1)
    expect($unreadFinishedSessionIds.get()).toContain(STORED)
  })
})
