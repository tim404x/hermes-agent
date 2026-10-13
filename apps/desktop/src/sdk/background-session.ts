/**
 * `host.openBackgroundSession` — a session that ARRIVED (a teammate's
 * `message_agent` side window), shown as a tab with an unread badge and
 * nothing else: no navigation, no focus move, no resume, no gateway dial. The
 * user opens it when they choose; closing the tab only closes the tab.
 */

import { probeStoredSession } from '@/app/session/hooks/use-session-actions/utils'
import { activeGatewayConnectionId } from '@/store/gateway'
import { normalizeProfileKey } from '@/store/profile'
import { $connection, $sessions, lineageAliases, setSessionOwnerHint } from '@/store/session'
import { stackBackgroundSessionTile } from '@/store/session-background-tile'
import { $focusedStoredSessionId } from '@/store/session-focus'
import { setSessionTileWorkspaceScope } from '@/store/session-states'
import { markSessionUnreadFinished } from '@/store/session-unread'
import { isMainWindow } from '@/store/windows'

export interface PluginBackgroundSessionOptions {
  /** Ids of the tab this one sits beside (a bot's Bot Chat: registry row +
   *  compression tip). The first one on screen anchors the new tab. */
  besideStoredIds?: readonly string[]
  /** The owning profile, as for `openSession`. */
  profile: string
  /** Exact cross-connection owner; absent = the profile on this connection. */
  route?: { connectionId: string; mode: 'local' | 'remote'; profile: string; targetProfile?: string }
  /** Bot Mode owner the tab belongs to (its workspace and caption scope). */
  workspaceOwnerKey: string
}

/** `'opened'` = a new background tab; `'open'` = already on screen (badged
 *  unless the user is looking at it); `null` = nothing to do here (no id, or a
 *  pop-out window, which has no tab strip). */
export function openBackgroundSession(
  storedSessionId: string,
  options: PluginBackgroundSessionOptions
): 'open' | 'opened' | null {
  const id = (storedSessionId ?? '').trim()
  const profile = (options.route?.profile ?? options.profile ?? '').trim()

  if (!id || !profile || !isMainWindow()) {
    return null
  }

  // Same owner record `openSession` keeps for a Bot tab: the tile's route is
  // what the session-request router dials once the user activates it.
  const connection = $connection.get()
  const remote = connection?.mode === 'remote'
  const connectionId = (remote && String(connection?.connectionId ?? '').trim()) || activeGatewayConnectionId()

  const ownerRoute = options.route
    ? { ...options.route }
    : connectionId
      ? { connectionId, mode: remote ? ('remote' as const) : ('local' as const), profile: normalizeProfileKey(profile) }
      : undefined

  if (ownerRoute) {
    setSessionOwnerHint(id, ownerRoute)
  }

  const scope = { ownerRoute, workspaceMode: 'bots' as const, workspaceOwnerKey: options.workspaceOwnerKey }
  const placed = stackBackgroundSessionTile(id, scope, options.besideStoredIds ?? [])

  // The tab the user is reading is never badged: the new turn is in front of them.
  if (!lineageAliases(id, $sessions.get()).includes($focusedStoredSessionId.get() ?? '')) {
    markSessionUnreadFinished(id, profile)
  }

  if (placed === 'opened') {
    // Remembered as a bot chat, like every Bot tab `openSession` opens. An
    // already-open tab keeps the workspace the user put it in.
    setSessionTileWorkspaceScope(id, scope)
    // A background tab never mounts, so nothing else resolves its row: without
    // the probe its caption reads "New session" until the first click.
    void probeStoredSession(id, ownerRoute).catch(() => undefined)
  }

  return placed
}
