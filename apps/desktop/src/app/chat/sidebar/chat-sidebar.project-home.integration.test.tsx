// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { setSidebarAgentsGrouped } from '@/store/layout'
import { exitProjectScope } from '@/store/project-scope'
import { $projectTree } from '@/store/projects'
import { $selectedStoredSessionId, $sessions } from '@/store/session'
import { $removedSessionIds } from '@/store/session-removal'
import { makeSessionInfo } from '@/test/session-info'

import { NO_PROJECT_ID, type SidebarProjectTree } from './projects/workspace-groups'

import { ChatSidebar } from './index'

// The grouped sidebar's one authority for membership is the backend
// `projects.tree`. Its synthetic Home bucket (`isNoProject`) already carries
// every session no project claimed, so the grouped overview needs no second,
// flat "Ungrouped" list under it: a row belongs to exactly one door.

const noop = () => {}

const noopAsync = async () => {}

const orphanOne = makeSessionInfo({ id: 'orphan-one', last_active: 3, started_at: 1, title: 'Orphan one' })

const orphanTwo = makeSessionInfo({ id: 'orphan-two', last_active: 2, started_at: 1, title: 'Orphan two' })

const appOne = makeSessionInfo({
  cwd: '/repos/app',
  git_repo_root: '/repos/app',
  id: 'app-one',
  last_active: 1,
  started_at: 1,
  title: 'App one'
})

// Shaped like `tui_gateway/project_tree.py` builds it: Home leads, lanes are
// empty until the drill-in hydrates, `previewSessions`/`sessionIds` carry the
// backend's placement of every row.
const backendTree: SidebarProjectTree[] = [
  {
    id: NO_PROJECT_ID,
    isNoProject: true,
    label: 'Home',
    path: null,
    previewSessions: [orphanOne, orphanTwo],
    repos: [
      {
        groups: [{ id: NO_PROJECT_ID, label: 'Home', path: null, sessions: [] }],
        id: NO_PROJECT_ID,
        label: 'Home',
        path: null,
        sessionCount: 2
      }
    ],
    sessionCount: 2,
    sessionIds: ['orphan-one', 'orphan-two']
  },
  {
    id: 'p_app',
    label: 'App',
    path: '/repos/app',
    previewSessions: [appOne],
    repos: [
      {
        groups: [{ id: '/repos/app::branch::', label: 'main', path: '/repos/app', sessions: [] }],
        id: '/repos/app',
        label: 'App',
        path: '/repos/app',
        sessionCount: 1
      }
    ],
    sessionCount: 1,
    sessionIds: ['app-one']
  }
]

const renderSidebar = () =>
  render(
    <MemoryRouter initialEntries={['/']}>
      <SidebarProvider>
        <ChatSidebar
          currentView="chat"
          onArchiveSession={noop}
          onBranchSession={noop}
          onDeleteSession={noop}
          onLoadMoreSessions={noop}
          onManageCronJob={noop}
          onNavigate={noop}
          onNewSessionInWorkspace={noop}
          onNewSessionSplit={noop}
          onResumeSession={noop}
          onRetrySessions={noop}
          onTriggerCronJob={noopAsync}
        />
      </SidebarProvider>
    </MemoryRouter>
  )

const sessionTitles = ['Orphan one', 'Orphan two', 'App one']

/** How many times each session row is painted anywhere in the sidebar. */
const rowCounts = () => Object.fromEntries(sessionTitles.map(title => [title, screen.queryAllByText(title).length]))

describe('ChatSidebar grouped by project', () => {
  beforeEach(() => {
    setSidebarAgentsGrouped(true)
    exitProjectScope()
    $projectTree.set(backendTree)
    $sessions.set([orphanOne, orphanTwo, appOne])
    $selectedStoredSessionId.set(null)
    $removedSessionIds.set(new Set())
  })

  afterEach(() => {
    cleanup()
    setSidebarAgentsGrouped(false)
    exitProjectScope()
    $projectTree.set([])
    $sessions.set([])
    $removedSessionIds.set(new Set())
  })

  it('paints every session once, under its project, with no Ungrouped section beneath the overview', () => {
    renderSidebar()

    // Native Home and the real project are both doors in the overview.
    expect(screen.getByRole('button', { name: 'Open Home' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Open App' })).toBeTruthy()

    // The custom flat list is gone — no extra section header...
    expect(screen.queryByText('Ungrouped')).toBeNull()
    // ...and no row painted twice (once under its project, again in a flat
    // list). Home's previews ARE the orphan rows, so nothing goes missing.
    expect(rowCounts()).toEqual({ 'Orphan one': 1, 'Orphan two': 1, 'App one': 1 })
  })

  it('opens Home to its orphan sessions and comes back to the overview', () => {
    renderSidebar()

    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Open Home' }))
    })

    // Inside Home: only the rows no project claimed, each exactly once.
    expect(rowCounts()).toEqual({ 'Orphan one': 1, 'Orphan two': 1, 'App one': 0 })
    expect(screen.queryByRole('button', { name: 'Open App' })).toBeNull()

    act(() => {
      fireEvent.click(screen.getByText('All projects'))
    })

    // Back at the overview: both doors, every row once again.
    expect(screen.getByRole('button', { name: 'Open Home' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Open App' })).toBeTruthy()
    expect(rowCounts()).toEqual({ 'Orphan one': 1, 'Orphan two': 1, 'App one': 1 })
  })
})
