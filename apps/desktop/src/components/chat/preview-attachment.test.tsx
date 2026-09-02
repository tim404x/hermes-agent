import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PreviewAttachment } from '@/components/chat/preview-attachment'
import type * as LocalPreview from '@/lib/local-preview'
import type * as PreviewStore from '@/store/preview'

// #101683 regression coverage: the delivered-file card must branch on the
// target's LOCAL filesystem state — an existing directory opens natively with
// no Download and no broken preview, an existing local file reveals instead of
// re-downloading, a remote-backend file keeps the gateway-backed actions, and a
// missing path reports instead of opening anything.
//
// The card's open affordance hands the target to the user's REAL default
// browser (`openPreviewTargetInBrowser`), never the in-app preview pane, so the
// assertions below check that bridge and that `@/store/preview` stays untouched.

const { isDesktopFsRemoteMode, notifyError, openPreview, openPreviewTargetInBrowser } = vi.hoisted(() => ({
  isDesktopFsRemoteMode: vi.fn(() => false),
  notifyError: vi.fn(),
  openPreview: vi.fn(),
  openPreviewTargetInBrowser: vi.fn(async () => {})
}))

vi.mock('@/app/chat/session-view', () => ({
  useSessionView: () => ({ $cwd: { get: () => '/work', listen: () => () => {}, subscribe: () => () => {} } })
}))

vi.mock('@/lib/desktop-fs', () => ({
  isDesktopFsRemoteMode,
  readDesktopDir: vi.fn(),
  readDesktopFileDataUrl: vi.fn(),
  readDesktopFileText: vi.fn()
}))

vi.mock('@/lib/local-preview', async importOriginal => ({
  ...(await importOriginal<typeof LocalPreview>()),
  normalizeOrLocalPreviewTarget: vi.fn(async (target: string) => ({
    kind: 'file' as const,
    label: 'report.pdf',
    source: target,
    url: `file://${target}`
  })),
  openPreviewTargetInBrowser
}))

vi.mock('@/store/notifications', () => ({ notifyError }))

vi.mock('@/store/preview', async importOriginal => ({
  ...(await importOriginal<typeof PreviewStore>()),
  openPreview
}))

const previousDesktop = window.hermesDesktop

function fileTarget(path: string, previewKind = 'text') {
  return {
    kind: 'file' as const,
    label: path.split('/').filter(Boolean).pop() || path,
    path,
    previewKind,
    source: `file://${path}`,
    url: `file://${path}`
  }
}

function mountDesktopStub(normalizePreviewTarget: (target: string, baseDir?: string) => unknown) {
  const openDir = vi.fn(async () => ({ ok: true }))
  const revealPath = vi.fn(async () => true)

  window.hermesDesktop = {
    normalizePreviewTarget: vi.fn(normalizePreviewTarget),
    openDir,
    revealPath
  } as never

  return { openDir, revealPath }
}

async function buttons() {
  return (await screen.findAllByRole('button')).map(button => button.textContent)
}

describe('PreviewAttachment local target classification (#101683)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    isDesktopFsRemoteMode.mockReturnValue(false)
  })

  afterEach(() => {
    cleanup()
    window.hermesDesktop = previousDesktop
  })

  it('opens a Windows file:///C:/... directory link natively with no Download or preview action', async () => {
    const { openDir, revealPath } = mountDesktopStub(() => fileTarget('C:/Users/E/reports/historical', 'directory'))

    render(<PreviewAttachment target="file:///C:/Users/E/reports/historical" />)

    const open = await screen.findByRole('button', { name: 'Open containing folder' })
    expect(await buttons()).toEqual(['Open containing folder'])

    await act(async () => {
      fireEvent.click(open)
    })

    expect(openDir).toHaveBeenCalledWith('C:/Users/E/reports/historical')
    expect(revealPath).not.toHaveBeenCalled()
    expect(openPreviewTargetInBrowser).not.toHaveBeenCalled()
    expect(openPreview).not.toHaveBeenCalled()
  })

  it('opens a POSIX directory link through the native folder action', async () => {
    const { openDir } = mountDesktopStub(() => fileTarget('/work/reports/historical', 'directory'))

    render(<PreviewAttachment target="/work/reports/historical" />)

    const open = await screen.findByRole('button', { name: 'Open containing folder' })

    await act(async () => {
      fireEvent.click(open)
    })

    expect(openDir).toHaveBeenCalledWith('/work/reports/historical')
    expect(openPreviewTargetInBrowser).not.toHaveBeenCalled()
    expect(openPreview).not.toHaveBeenCalled()
  })

  it('reveals an existing local file in the file manager instead of offering Download', async () => {
    const { openDir, revealPath } = mountDesktopStub(() => fileTarget('/work/report.md'))

    render(<PreviewAttachment target="/work/report.md" />)

    await screen.findByRole('button', { name: 'Open containing folder' })
    expect(await buttons()).toEqual(['Open containing folder', 'Open in browser'])

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Open containing folder' }))
    })

    expect(revealPath).toHaveBeenCalledWith('/work/report.md')
    expect(openDir).not.toHaveBeenCalled()
  })

  it('keeps Download and the open action for a remote-backend file', async () => {
    isDesktopFsRemoteMode.mockReturnValue(true)
    const { openDir, revealPath } = mountDesktopStub(() => fileTarget('/srv/report.md'))

    render(<PreviewAttachment target="/srv/report.md" />)

    expect(await buttons()).toEqual(['Download', 'Open in browser'])

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Open in browser' }))
    })

    // The remote path is not on this machine: no native folder action fires,
    // and the staged copy still lands in the real browser, not the in-app pane.
    expect(openDir).not.toHaveBeenCalled()
    expect(revealPath).not.toHaveBeenCalled()
    await waitFor(() => expect(openPreviewTargetInBrowser).toHaveBeenCalledTimes(1))
    expect(openPreview).not.toHaveBeenCalled()
  })

  it('reports a missing path instead of handing the browser a dead path', async () => {
    mountDesktopStub(() => fileTarget('/work/gone.md', 'missing'))

    render(<PreviewAttachment target="/work/gone.md" />)

    const preview = await screen.findByRole('button', { name: 'Preview unavailable' })

    await act(async () => {
      fireEvent.click(preview)
    })

    expect(notifyError).toHaveBeenCalled()
    expect(openPreviewTargetInBrowser).not.toHaveBeenCalled()
    expect(openPreview).not.toHaveBeenCalled()
  })
})

// Attachment links must open in the user's REAL default browser, never the
// in-app preview pane. An attachment is a finished artifact (a PDF, a rendered
// report) that the user goes on to print, annotate, or share from the browser
// they live in, so landing it in the embedded pane was a dead end they had to
// escape from on every doc.
//
// This asserts the BEHAVIOR (the external-browser bridge is invoked and the
// preview store is left alone), not the button's wording.
describe('PreviewAttachment', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    isDesktopFsRemoteMode.mockReturnValue(false)
  })

  afterEach(() => {
    cleanup()
    window.hermesDesktop = previousDesktop
  })

  it('opens the attachment in the external browser, not the in-app pane', async () => {
    render(<PreviewAttachment target="/work/report.pdf" />)

    fireEvent.click(screen.getByRole('button', { name: 'Open in browser' }))

    await waitFor(() => expect(openPreviewTargetInBrowser).toHaveBeenCalledTimes(1))
    // The in-app preview store must never be touched by an attachment click.
    expect(openPreview).not.toHaveBeenCalled()
  })

  it('never offers a Hide affordance, because nothing is opened in-app', () => {
    render(<PreviewAttachment target="/work/report.pdf" />)

    expect(screen.queryByRole('button', { name: 'Hide' })).toBeNull()
  })
})
