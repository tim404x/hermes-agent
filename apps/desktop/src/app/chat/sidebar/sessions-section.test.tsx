import { cleanup, render } from '@testing-library/react'
import type * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'

import { SidebarSessionsSection } from './sessions-section'

afterEach(cleanup)

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        dateDivider: {
          earlierThisMonth: 'Earlier this month',
          lastMonth: 'Last month',
          lastWeek: 'Last week',
          older: 'Older',
          today: 'Today',
          yesterday: 'Yesterday'
        }
      }
    }
  })
}))

vi.mock('./session-row', () => ({
  SidebarSessionRow: ({ session }: { session: SessionInfo }) => (
    <div data-testid={`session-row-${session.id}`}>{session.id}</div>
  )
}))

// The overview row itself has its own tests; here it is a pass-through that
// hands the section's OWN renderRows the pre-ranked preview rows, so the
// assertion below runs the real section render pipeline.
vi.mock('./projects/overview-row', () => ({
  ProjectBackRow: () => null,
  ProjectOverviewRow: ({
    previewSessions,
    renderRows
  }: {
    previewSessions?: SessionInfo[]
    renderRows?: (rows: SessionInfo[]) => React.ReactNode
  }) => <div data-testid="overview-row">{renderRows?.(previewSessions ?? [])}</div>
}))

function makeSession(id: string, startedAt = 1000): SessionInfo {
  return {
    handoff_platform: null,
    handoff_state: null,
    id,
    last_active: startedAt,
    profile: 'default',
    started_at: startedAt
  } as unknown as SessionInfo
}

const noop = () => {}

describe('SidebarSessionsSection project surfaces keep the ranked order', () => {
  // Project rows arrive already ranked by the backend / overlay
  // (compareProjectRank: pin-in-project first, then recency). The section must
  // render them in that order. On first ship its row helper re-sorted roots
  // by recency and silently put the pinned chat back in its date slot — the
  // feature was wired end to end and still did nothing visible.
  const project = { id: 'p1', isNoProject: false, label: 'P', path: '/p', repos: [], sessionCount: 2 } as never

  const renderedOrder = (container: HTMLElement) =>
    Array.from(container.querySelectorAll('[data-testid^="session-row-"]')).map(el => el.textContent)

  it('overview preview: a project-pinned row stays above a fresher unpinned one', () => {
    const pinned = { ...makeSession('pinned', 10), project_pinned_at: 100 } as SessionInfo
    const fresh = makeSession('fresh', 999)

    const { container } = render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        projectOverview={[project]}
        projectOverviewPreviews={{ p1: [pinned, fresh] }}
        sessions={[]}
      />
    )

    expect(renderedOrder(container)).toEqual(['pinned', 'fresh'])
  })

  it('overview preview: unpinned rows still nest branch children under their parent', () => {
    const parent = makeSession('parent', 50)
    const child = { ...makeSession('child', 60), parent_session_id: 'parent' } as SessionInfo
    const other = makeSession('other', 55)

    const { container } = render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        onToggleUnread={noop}
        open={true}
        pinned={false}
        projectOverview={[project]}
        projectOverviewPreviews={{ p1: [parent, other, child] }}
        sessions={[]}
      />
    )

    // Caller order kept for roots; the branch still travels with its parent.
    expect(renderedOrder(container)).toEqual(['parent', 'child', 'other'])
  })
})
