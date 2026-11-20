// Desktop polish batch 2 (P3a): a hermes://open/<session id> link is a real tab, never a swap of
// the chat on screen. Before, the deep-link handler navigated the CURRENT tab, so a bot opening five
// task chats in a row left only the last one and replaced whatever the user was reading.
import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { sessionRoute } from '../../routes'

import { useDesktopIntegrations } from './use-desktop-integrations'

const openSession = vi.hoisted(() => vi.fn())

vi.mock('@/app/open-session', () => ({ openSession }))

type DeepLinkListener = (payload: { kind: string; name?: string; params?: Record<string, string> }) => void

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

describe('hermes://open deep links', () => {
  let navigate: ReturnType<typeof vi.fn<(...args: unknown[]) => void>>
  let deliver: DeepLinkListener

  beforeEach(() => {
    window.localStorage.clear()
    openSession.mockClear()
    navigate = vi.fn()

    desktopWindow.hermesDesktop = {
      setPreviewShortcutActive: vi.fn(),
      onOpenUpdatesRequested: vi.fn(),
      onFocusSession: vi.fn(),
      onNotificationAction: vi.fn(),
      onNotificationActivate: vi.fn(),
      onDeepLink: vi.fn((listener: DeepLinkListener) => {
        deliver = listener

        return () => undefined
      }),
      signalDeepLinkReady: vi.fn(),
      onClosePreviewRequested: vi.fn(),
      onOpenFolderRequested: vi.fn(),
      api: vi.fn(async () => {
        throw new Error('no api stub for this test')
      })
    } as unknown as Window['hermesDesktop']

    renderHook(() =>
      useDesktopIntegrations({
        activeProfile: 'default',
        chatOpen: false,
        hasPreview: false,
        locationPathname: sessionRoute('chat-on-screen'),
        navigate,
        profileReady: false,
        refreshSessions: vi.fn(),
        resumeExhaustedSessionId: null,
        resumeLastSession: false,
        routedSessionId: 'chat-on-screen',
        runtimeIdByStoredSessionId: { current: new Map() },
        sessions: []
      })
    )
  })

  afterEach(() => {
    if (initialHermesDesktop) {
      desktopWindow.hermesDesktop = initialHermesDesktop
    }

    vi.restoreAllMocks()
  })

  it('opens each linked chat as its own tab and never navigates the chat on screen', () => {
    for (const id of ['local-task-a', 'local-task-b', '20261119_220431_6f05b2']) {
      deliver({ kind: 'open', name: id, params: {} })
    }

    expect(openSession.mock.calls.map(([id, , intent]) => [id, intent])).toEqual([
      ['local-task-a', 'tab'],
      ['local-task-b', 'tab'],
      ['20261119_220431_6f05b2', 'tab']
    ])
    expect(navigate).not.toHaveBeenCalled()
  })

  it('keeps the in-place open when the link asks for it', () => {
    deliver({ kind: 'open', name: 'local-task-a', params: { in: 'current' } })

    expect(openSession).not.toHaveBeenCalled()
    expect(navigate).toHaveBeenCalledWith(expect.stringContaining(sessionRoute('local-task-a')))
  })

  it('still navigates a link to an app page', () => {
    deliver({ kind: 'open', name: 'settings', params: {} })

    expect(openSession).not.toHaveBeenCalled()
    expect(navigate).toHaveBeenCalledWith('/settings')
  })
})
