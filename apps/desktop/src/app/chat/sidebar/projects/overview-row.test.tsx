import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import type * as ProjectsStore from '@/store/projects'

import type * as Model from './model'
import { ProjectOverviewRow } from './overview-row'
import type { SidebarProjectTree } from './workspace-groups'

afterEach(cleanup)

const workspaceOpen = vi.hoisted(() => ({ value: false }))

const projectsStore = vi.hoisted(() => ({
  fetchProjectSessions:
    vi.fn<(id: string, options?: { supersedable?: boolean }) => Promise<null | SidebarProjectTree>>(),
  projectProfile: vi.fn<() => null | string>(() => 'default')
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        newSessionIn: (label: string) => `New session in ${label}`,
        projects: {
          enter: (label: string) => `Enter ${label}`,
          reorder: (label: string) => `Reorder ${label}`,
          toggle: (label: string, open: boolean) => `${open ? 'Show' : 'Hide'} ${label} sessions`,
          showAllCount: (count: number) => `Show all ${count} sessions`,
          autoDiscovered: 'Auto-discovered'
        },
        showMoreIn: (count: number, label: string) => `Show ${count} more in ${label}`
      }
    }
  })
}))

vi.mock('@/store/projects', async importOriginal => ({
  ...(await importOriginal<typeof ProjectsStore>()),
  ...projectsStore
}))

// Keep the pure helpers real (they are the logic under test); stub only the
// persisted open/collapse hook, the in-memory fallback preview, and the
// density-measured window height — so the preview-window cases can assert a
// fixed px maxHeight without depending on the active density.
vi.mock('./model', async () => ({
  ...(await vi.importActual<typeof Model>('./model')),
  latestProjectSessions: () => [],
  previewWindowMaxHeight: () => '86px',
  useWorkspaceNodeOpen: () => [workspaceOpen.value, vi.fn()]
}))

// ProjectMenu (the kebab) has its own dedicated test file — stub it here so
// this file only exercises overview-row's own Tip usage (the disclosure
// toggle) plus the WorkspaceAddButton wiring. ProjectContextMenu (the row's
// right-click wrapper) is stubbed as a pass-through so the row still renders.
vi.mock('./project-menu', () => ({
  ProjectContextMenu: ({ children }: { children: ReactNode }) => children,
  ProjectMenu: () => null
}))

const project = { id: 'p1', label: 'Test D' } as unknown as SidebarProjectTree

const session = (id: string, updated: number): SessionInfo => ({ id, updated_at: updated }) as unknown as SessionInfo

describe('ProjectOverviewRow', () => {
  afterEach(() => {
    workspaceOpen.value = false
    projectsStore.fetchProjectSessions.mockReset()
    projectsStore.projectProfile.mockReset().mockReturnValue('default')
  })

  it('does not render the disclosure toggle when there is nothing to preview', () => {
    render(<ProjectOverviewRow project={project} />)

    expect(screen.queryByRole('button', { name: 'Show Test D sessions' })).toBeNull()
  })

  // Group by → Projects previews only the 3 most-recent sessions per project;
  // sessions 4+ need a visible, in-place way to be reached (#112406).
  it('offers "Show all N sessions" past the preview cap and reveals the rest of the project inline', async () => {
    workspaceOpen.value = true
    const five = Array.from({ length: 5 }, (_, index) => session(`s${index + 1}`, 500 - index))
    const busy = { ...project, sessionCount: 5 } as SidebarProjectTree
    projectsStore.fetchProjectSessions.mockResolvedValue({
      ...busy,
      repos: [{ groups: [{ sessions: five }] }]
    } as unknown as SidebarProjectTree)

    render(
      <ProjectOverviewRow
        previewSessions={five.slice(0, 3)}
        project={busy}
        renderRows={items => <div data-testid="rows">{items.map(item => item.id).join(',')}</div>}
      />
    )

    expect(screen.getByTestId('rows').textContent).toBe('s1,s2,s3')

    fireEvent.click(screen.getByRole('button', { name: 'Show all 5 sessions' }))

    await waitFor(() => expect(screen.getByTestId('rows').textContent).toBe('s1,s2,s3,s4,s5'))
    expect(projectsStore.fetchProjectSessions).toHaveBeenCalledWith('p1', { supersedable: false })
    expect(screen.queryByRole('button', { name: 'Show all 5 sessions' })).toBeNull()
  })

  // A project with hundreds of chats hydrates them all, but the overview must
  // not mount every row at once: it reveals them a page at a time, with a
  // labeled row to the next page, until every session is on screen (#70421).
  it('pages a large hydrated project instead of mounting every session at once', async () => {
    workspaceOpen.value = true
    const all = Array.from({ length: 120 }, (_, index) => session(`s${index + 1}`, 1000 - index))
    const busy = { ...project, sessionCount: 120 } as SidebarProjectTree
    projectsStore.fetchProjectSessions.mockResolvedValue({
      ...busy,
      repos: [{ groups: [{ sessions: all }] }]
    } as unknown as SidebarProjectTree)

    render(
      <ProjectOverviewRow
        previewSessions={all.slice(0, 3)}
        project={busy}
        renderRows={items => <div data-testid="rows">{items.map(item => item.id).join(',')}</div>}
      />
    )

    const shown = () => screen.getByTestId('rows').textContent?.split(',').length

    fireEvent.click(screen.getByText('Show all 120 sessions'))

    await waitFor(() => expect(shown()).toBe(50))
    fireEvent.click(screen.getByText('Show 50 more in Test D'))
    expect(shown()).toBe(100)
    fireEvent.click(screen.getByText('Show 20 more in Test D'))
    expect(shown()).toBe(120)
    expect(screen.queryByText(/Show .* more in Test D/)).toBeNull()
  })

  // The hydrated lanes are the raw backend payload: pinned, filtered-out and
  // just-deleted sessions must go through the same exclusion the previews did,
  // and N must not promise rows the view hides.
  it('"Show all" runs the hydrated lanes through the tree exclusion and counts only what it will render', async () => {
    workspaceOpen.value = true
    const five = Array.from({ length: 5 }, (_, index) => session(`s${index + 1}`, 500 - index))
    const busy = { ...project, sessionCount: 5 } as SidebarProjectTree
    projectsStore.fetchProjectSessions.mockResolvedValue({
      ...busy,
      repos: [{ groups: [{ sessions: five }] }]
    } as unknown as SidebarProjectTree)
    // s2 is pinned (renders in Pinned), s5 was just deleted.
    const hidden = new Set(['s2', 's5'])

    render(
      <ProjectOverviewRow
        hiddenSessionCount={hidden.size}
        isSessionHidden={item => hidden.has(item.id)}
        previewSessions={[five[0], five[2]]}
        project={busy}
        renderRows={items => <div data-testid="rows">{items.map(item => item.id).join(',')}</div>}
      />
    )

    fireEvent.click(screen.getByRole('button', { name: 'Show all 3 sessions' }))

    await waitFor(() => expect(screen.getByTestId('rows').textContent).toBe('s1,s3,s4'))
  })

  it('offers the "new session" add button on Home, which starts one with no folder', () => {
    const home = {
      id: '__no_project__',
      isNoProject: true,
      label: 'Home',
      path: null
    } as unknown as SidebarProjectTree

    const onNewSession = vi.fn()

    render(<ProjectOverviewRow onNewSession={onNewSession} project={home} />)
    fireEvent.click(screen.getByRole('button', { name: 'New session in Home' }))

    expect(onNewSession).toHaveBeenCalledWith(null)
  })

  describe('preview window', () => {
    const sessions = (n: number) => Array.from({ length: n }, (_, i) => ({ id: `s${i}` }) as unknown as SessionInfo)

    // The nest is the only element that carries both rows and a maxHeight.
    const nest = (container: HTMLElement) => container.querySelector<HTMLElement>('[style*="max-height"]')

    beforeEach(() => {
      workspaceOpen.value = true
    })

    afterEach(() => {
      workspaceOpen.value = false
    })

    it('renders every loaded preview row, not just the three that fit', () => {
      const renderRows = vi.fn((_rows: SessionInfo[]) => null)

      render(<ProjectOverviewRow previewSessions={sessions(8)} project={project} renderRows={renderRows} />)

      expect(renderRows.mock.calls.at(-1)?.[0]).toHaveLength(8)
    })

    it('caps the preview at a fixed window and scrolls it once past three rows', () => {
      const { container } = render(
        <ProjectOverviewRow previewSessions={sessions(8)} project={project} renderRows={() => null} />
      )

      const window = nest(container)
      expect(window).toBeTruthy()
      expect(window?.style.maxHeight).toBe('86px')
      expect(window?.className).toContain('overflow-y-auto')
    })

    it('leaves a short preview unbounded — no window, no scroller', () => {
      const { container } = render(
        <ProjectOverviewRow previewSessions={sessions(3)} project={project} renderRows={() => null} />
      )

      expect(nest(container)).toBeNull()
    })

    it('never traps the wheel: the window must not contain its overscroll', () => {
      // The nest sits INSIDE the sidebar's own scroller — containing overscroll
      // here kills wheel chaining at the list's ends (#84964).
      const { container } = render(
        <ProjectOverviewRow previewSessions={sessions(8)} project={project} renderRows={() => null} />
      )

      expect(nest(container)?.className).not.toContain('overscroll-contain')
    })
  })
})
