import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type * as LocalPreview from '@/lib/local-preview'
import type * as PreviewStore from '@/store/preview'

import { PreviewAttachment } from './preview-attachment'

// Attachment links must open in the user's REAL default browser, never the
// in-app preview pane. An attachment is a finished artifact (a PDF, a rendered
// report) that the user goes on to print, annotate, or share from the browser
// they live in, so landing it in the embedded pane was a dead end they had to
// escape from on every doc.
//
// This asserts the BEHAVIOR (the external-browser bridge is invoked and the
// preview store is left alone), not the button's wording.
const openPreviewInBrowser = vi.fn(async () => {})
const openPreview = vi.fn()

vi.mock('@/app/chat/session-view', () => ({
  useSessionView: () => ({ $cwd: { get: () => '/work', listen: () => () => {}, subscribe: () => () => {} } })
}))

vi.mock('@/lib/local-preview', async importOriginal => {
  const actual = await importOriginal<typeof LocalPreview>()

  return {
    ...actual,
    normalizeOrLocalPreviewTarget: vi.fn(async (target: string) => ({
      kind: 'file' as const,
      label: 'report.pdf',
      source: target,
      url: `file://${target}`
    })),
    openPreviewTargetInBrowser: vi.fn(async () => {
      await openPreviewInBrowser()
    })
  }
})

vi.mock('@/store/preview', async importOriginal => {
  const actual = await importOriginal<typeof PreviewStore>()

  return { ...actual, openPreview }
})

describe('PreviewAttachment', () => {
  afterEach(() => {
    cleanup()
    openPreviewInBrowser.mockClear()
    openPreview.mockClear()
  })

  it('opens the attachment in the external browser, not the in-app pane', async () => {
    render(<PreviewAttachment target="/work/report.pdf" />)

    fireEvent.click(screen.getByRole('button', { name: 'Open in browser' }))

    await waitFor(() => expect(openPreviewInBrowser).toHaveBeenCalledTimes(1))
    // The in-app preview store must never be touched by an attachment click.
    expect(openPreview).not.toHaveBeenCalled()
  })

  it('never offers a Hide affordance, because nothing is opened in-app', () => {
    render(<PreviewAttachment target="/work/report.pdf" />)

    expect(screen.queryByRole('button', { name: 'Hide' })).toBeNull()
  })
})
