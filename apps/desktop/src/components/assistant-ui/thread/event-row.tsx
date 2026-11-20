import { type FC, type ReactNode, useState } from 'react'

import { MessageTimelineTimestamp } from '@/components/assistant-ui/thread/timeline-timestamp'
import { SCAFFOLD_GLYPH_CLASS, SCAFFOLD_LABEL_CLASS, ScaffoldRow } from '@/components/chat/scaffold-row'
import { cn } from '@/lib/utils'

/**
 * One look for everything in the thread that happened rather than was said: a background job
 * finishing, a scheduled job's output, a bot's brief or message, a reply folded away, a model
 * switch. A quiet scaffold line in the reading column: a glyph, one short sentence, the time, and
 * the body one click away when there is one.
 *
 * Before this, each kind picked its own look (a centred 11px note for bot messages, a left
 * terminal row for processes, a centred grey line for model switches, the user's own bubble for
 * cron output), which read as four kinds of thing for what is one.
 */
export interface EventRowProps {
  /** The expanded body; a function renders lazily (only once opened). */
  detail?: (() => ReactNode) | ReactNode
  /** Glyph in the fixed leading cell, so every event shares one left edge. */
  icon?: ReactNode
  label: ReactNode
  /** Starts expanded (an inter-agent reply the user opens stays open across re-renders). */
  defaultOpen?: boolean
  slot?: string
  /** Show the message's timeline time after the label (off for events inside a tool part). */
  timestamp?: boolean
}

export const EventRow: FC<EventRowProps> = ({ defaultOpen = false, detail, icon, label, slot, timestamp = true }) => {
  const [open, setOpen] = useState(defaultOpen)
  const hasDetail = detail !== undefined && detail !== null && detail !== '' && detail !== false

  return (
    <div
      className="flex w-full min-w-0 flex-col self-start py-1 pl-(--message-text-indent)"
      data-event-row=""
      data-slot={slot ?? 'aui_event-row'}
    >
      <div data-conversation-scaffold="">
        <ScaffoldRow
          onToggle={hasDetail ? () => setOpen(!open) : undefined}
          open={open}
          trailing={
            timestamp ? (
              <>
                {' '}
                <MessageTimelineTimestamp />
              </>
            ) : undefined
          }
        >
          {icon && <span className={SCAFFOLD_GLYPH_CLASS}>{icon}</span>}
          <span className={cn(SCAFFOLD_LABEL_CLASS, 'min-w-0 truncate')}>{label}</span>
        </ScaffoldRow>
      </div>
      {open && hasDetail && (
        <div className="mt-2 min-w-0 max-w-full" data-slot="aui_event-row-detail">
          {typeof detail === 'function' ? (detail as () => ReactNode)() : detail}
        </div>
      )}
    </div>
  )
}

/** "Neo | Chief of Staff" -> "Neo": events name a bot by its short name. */
export function shortBotName(name: string): string {
  return name.split(/\s+[|\u00b7\u2022\u2014\u2013]\s+/)[0]?.trim() || name.trim()
}
