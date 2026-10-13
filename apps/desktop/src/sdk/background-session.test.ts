import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { paneMirror } from '@/app/chat/pane-mirror'
import { findGroupOfPane } from '@/components/pane-shell/tree/model'
import * as model from '@/components/pane-shell/tree/model'
import * as tree from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import * as session from '@/store/session'
import * as states from '@/store/session-states'

const { probeStoredSession } = vi.hoisted(() => ({
  probeStoredSession: vi.fn(async () => ({ status: 'inconclusive' as const }))
}))

vi.mock('@/app/session/hooks/use-session-actions/utils', async importOriginal => ({
  ...(await importOriginal<object>()),
  probeStoredSession
}))

const { openBackgroundSession } = await import('./background-session')

// host.openBackgroundSession: a teammate's side window shows up as an unread
// tab while the user keeps working; nothing navigates, fronts or resumes.

beforeAll(() => {
  const disposeWorkspace = registry.register({
    area: 'panes',
    data: { placement: 'main', uncloseable: true },
    id: 'workspace',
    render: () => null,
    title: 'Chat'
  })

  tree.watchContributedPanes()
  paneMirror({
    source: states.$sessionTiles,
    key: tile => tile.storedSessionId,
    prefix: 'session-tile',
    dir: tile => tile.dir,
    anchor: tile => tile.anchor,
    before: tile => tile.before,
    minWidth: '20rem',
    title: id => id,
    render: () => null,
    close: states.closeSessionTile
  })()

  return disposeWorkspace
})

afterEach(() => {
  for (const tile of states.$sessionTiles.get()) {
    states.discardSessionTile(tile.storedSessionId)
  }

  tree.$layoutTree.set(null)
  tree.$activeTreeGroup.set(null)
  session.$selectedStoredSessionId.set(null)
  session.$unreadFinishedSessionIds.set([])
  probeStoredSession.mockClear()
  window.localStorage.clear()
})

const route = { connectionId: 'local', mode: 'local' as const, profile: 'atlas', targetProfile: 'atlas' }
const options = { besideStoredIds: ['atlas-bot-chat'], profile: 'atlas', route, workspaceOwnerKey: 'bot:atlas' }

function userIsInMain() {
  tree.declareDefaultTree(model.group(['workspace'], { active: 'workspace', id: 'main' }))
  session.$selectedStoredSessionId.set('what-the-user-reads')
}

describe('openBackgroundSession', () => {
  it('adds an unread bot tab and leaves the user where they are', () => {
    userIsInMain()

    expect(openBackgroundSession('side-1', options)).toBe('opened')

    const layout = tree.$layoutTree.get()!
    const strip = findGroupOfPane(layout, 'session-tile:side-1')!

    expect(strip.active).toBe('workspace')
    expect(session.$selectedStoredSessionId.get()).toBe('what-the-user-reads')
    expect(session.$unreadFinishedSessionIds.get()).toContain('side-1')
    expect(states.$sessionTiles.get()).toEqual([
      expect.objectContaining({
        ownerRoute: route,
        storedSessionId: 'side-1',
        workspaceMode: 'bots',
        workspaceOwnerKey: 'bot:atlas'
      })
    ])
    // The never-mounted tab still gets its caption from the stored row.
    expect(probeStoredSession).toHaveBeenCalledWith('side-1', route)
  })

  it('a new DM into an open tab badges it again without a second tab', () => {
    userIsInMain()
    openBackgroundSession('side-1', options)
    session.$unreadFinishedSessionIds.set([])

    expect(openBackgroundSession('side-1', options)).toBe('open')

    expect(session.$unreadFinishedSessionIds.get()).toContain('side-1')
    expect(states.$sessionTiles.get()).toHaveLength(1)
  })

  it('never badges the tab the user is reading', () => {
    tree.declareDefaultTree(model.group(['workspace'], { active: 'workspace', id: 'main' }))
    session.$selectedStoredSessionId.set('side-1')

    expect(openBackgroundSession('side-1', options)).toBe('open')

    expect(session.$unreadFinishedSessionIds.get()).not.toContain('side-1')
  })

  it('closing the tab only drops the tab', () => {
    userIsInMain()
    openBackgroundSession('side-1', options)
    expect(states.$sessionTiles.get()).toHaveLength(1)

    states.closeSessionTile('side-1')

    expect(states.$sessionTiles.get()).toEqual([])
    expect(tree.$layoutTree.get() && findGroupOfPane(tree.$layoutTree.get()!, 'session-tile:side-1')).toBeFalsy()
  })
})
