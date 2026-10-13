/**
 * Background session tabs: a session that arrived on its own (a teammate's
 * `message_agent` side window) joins the tab strip without taking the
 * foreground. Opening is presentation only — no resume, no gateway dial, no
 * focus move: tile adoption is silent (`adoptContributedPanes`), and the tab's
 * pane mounts only when the user activates it.
 */

import { findGroupOfPane } from '@/components/pane-shell/tree/model'
import { $layoutTree } from '@/components/pane-shell/tree/store'

import { $selectedStoredSessionId, $sessions, lineageAliases } from './session'
import { TILE_PANE_PREFIX } from './session-focus'
import { $sessionTiles, openSessionTile, type SessionTileWorkspaceScope } from './session-states'

/** Where a background tab docks: right after the first of `besideStoredIds`
 *  that is on screen (its tile, or the main tab when main holds it), else
 *  after the owner's newest open tab. Empty when none is placed, which docks
 *  it the way any new tab docks. */
function backgroundTileSlot(
  besideStoredIds: readonly string[],
  workspaceOwnerKey: string | undefined
): { anchor?: string; before?: null | string } {
  const tiles = $sessionTiles.get()
  const besideTile = tiles.find(tile => besideStoredIds.includes(tile.storedSessionId))
  const ownTab = tiles.findLast(tile => tile.workspaceMode === 'bots' && tile.workspaceOwnerKey === workspaceOwnerKey)

  const anchor = besideTile
    ? `${TILE_PANE_PREFIX}${besideTile.storedSessionId}`
    : besideStoredIds.includes($selectedStoredSessionId.get() ?? '')
      ? 'workspace'
      : ownTab && `${TILE_PANE_PREFIX}${ownTab.storedSessionId}`

  const tree = $layoutTree.get()
  const group = anchor && tree ? findGroupOfPane(tree, anchor) : null

  if (!anchor || !group) {
    return {}
  }

  return { anchor, before: group.panes[group.panes.indexOf(anchor) + 1] ?? null }
}

/** Is any id of this conversation's lineage already a tab or the main chat? */
function sessionOnScreen(storedSessionId: string): boolean {
  const aliases = lineageAliases(storedSessionId, $sessions.get())

  return (
    aliases.includes($selectedStoredSessionId.get() ?? '') ||
    $sessionTiles.get().some(tile => aliases.includes(tile.storedSessionId))
  )
}

/** Stack `storedSessionId` as a background tab beside `besideStoredIds` (a
 *  bot's Bot Chat ids: registry row + compression tip). `'open'` when the
 *  conversation is already on screen — nothing moves, the caller only badges
 *  it; `'opened'` when a new tab was added. Never fronts the tab and never
 *  marks the session read: the caller decides the unread state. */
export function stackBackgroundSessionTile(
  storedSessionId: string,
  scope: SessionTileWorkspaceScope,
  besideStoredIds: readonly string[] = []
): 'open' | 'opened' {
  if (sessionOnScreen(storedSessionId)) {
    return 'open'
  }

  const { anchor, before } = backgroundTileSlot(besideStoredIds, scope.workspaceOwnerKey)

  openSessionTile(storedSessionId, 'center', anchor, before, scope)

  return 'opened'
}
