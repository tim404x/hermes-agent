import { afterEach, beforeAll, describe, expect, it } from 'vitest'

import { paneMirror } from '@/app/chat/pane-mirror'
import { findGroupOfPane } from '@/components/pane-shell/tree/model'
import * as model from '@/components/pane-shell/tree/model'
import * as tree from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import * as session from '@/store/session'
import { stackBackgroundSessionTile } from '@/store/session-background-tile'
import * as states from '@/store/session-states'

// A teammate's message_agent side window arrives while the user is busy
// elsewhere: it must show up as a tab beside that bot's Bot Chat, never take
// the tab the user is in, and never stack twice.

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

const owner = {
  ownerRoute: { connectionId: 'local', mode: 'local' as const, profile: 'atlas' },
  workspaceMode: 'bots' as const,
  workspaceOwnerKey: 'bot:local::atlas'
}

afterEach(() => {
  for (const tile of states.$sessionTiles.get()) {
    states.discardSessionTile(tile.storedSessionId)
  }

  tree.$layoutTree.set(null)
  tree.$activeTreeGroup.set(null)
  session.$selectedStoredSessionId.set(null)
  window.localStorage.clear()
})

function stripOf(paneId: string) {
  const layout = tree.$layoutTree.get()
  const group = layout ? findGroupOfPane(layout, paneId) : null

  expect(group).toBeTruthy()

  return group!
}

/** The user's strip: the atlas Bot Chat tab, then a tab they are working in. */
function withBotChatAndAWorkTab() {
  tree.declareDefaultTree(model.group(['workspace'], { active: 'workspace', id: 'main' }))
  session.$selectedStoredSessionId.set('sessions-main')
  states.openSessionTile('atlas-bot-chat', 'center', 'workspace', undefined, {
    ...owner,
    workspaceTabTitle: 'Bot Chat'
  })
  states.openSessionTile('work-tab', 'center', 'session-tile:atlas-bot-chat')
  tree.activateTreePane(stripOf('session-tile:work-tab').id, 'session-tile:work-tab')
}

describe('stackBackgroundSessionTile', () => {
  it('lands right after the bot chat tab and leaves the active tab alone', () => {
    withBotChatAndAWorkTab()

    expect(stackBackgroundSessionTile('side-1', owner, ['atlas-bot-chat-root', 'atlas-bot-chat'])).toBe('opened')

    const strip = stripOf('session-tile:side-1')

    expect(strip.panes).toEqual([
      'workspace',
      'session-tile:atlas-bot-chat',
      'session-tile:side-1',
      'session-tile:work-tab'
    ])
    expect(strip.active).toBe('session-tile:work-tab')
    expect(states.$sessionTiles.get().find(tile => tile.storedSessionId === 'side-1')).toMatchObject({
      ownerRoute: owner.ownerRoute,
      workspaceMode: 'bots',
      workspaceOwnerKey: owner.workspaceOwnerKey
    })
  })

  it('never stacks a second tab for a window that is already open', () => {
    withBotChatAndAWorkTab()
    stackBackgroundSessionTile('side-1', owner, ['atlas-bot-chat'])

    expect(stackBackgroundSessionTile('side-1', owner, ['atlas-bot-chat'])).toBe('open')

    expect(states.$sessionTiles.get().filter(tile => tile.storedSessionId === 'side-1')).toHaveLength(1)
    expect(stripOf('session-tile:side-1').active).toBe('session-tile:work-tab')
  })

  it('counts the chat loaded in main as already open', () => {
    tree.declareDefaultTree(model.group(['workspace'], { active: 'workspace', id: 'main' }))
    session.$selectedStoredSessionId.set('side-1')

    expect(stackBackgroundSessionTile('side-1', owner, ['atlas-bot-chat'])).toBe('open')
    expect(states.$sessionTiles.get()).toEqual([])
  })

  it('sits beside the main tab when the bot chat lives in main', () => {
    tree.declareDefaultTree(model.group(['workspace'], { active: 'workspace', id: 'main' }))
    session.$selectedStoredSessionId.set('atlas-bot-chat')
    states.openSessionTile('work-tab', 'center', 'workspace')

    stackBackgroundSessionTile('side-1', owner, ['atlas-bot-chat'])

    expect(stripOf('session-tile:side-1').panes).toEqual(['workspace', 'session-tile:side-1', 'session-tile:work-tab'])
    expect(stripOf('workspace').active).toBe('workspace')
  })
})
