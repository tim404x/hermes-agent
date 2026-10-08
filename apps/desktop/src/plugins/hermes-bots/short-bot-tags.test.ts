/**
 * Short @tags for "Name | Role" titles (Tim, 8 Oct 2026: "@atlas instead of
 * @atlas-growth-lead"). A bot titled "Atlas | Growth Lead" is offered,
 * introduced and resolved as @atlas; the whole-title tag keeps resolving, and
 * a short name that would be ambiguous falls back to the whole title so a
 * mention never lands on the wrong bot.
 *
 * Drives the real `data`, `group-rounds` and `group-round-prompt` modules;
 * only the SDK host is proxied, as in cross-connection-bots.test.ts.
 */
import type * as HermesSdk from '@hermes/plugin-sdk'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { GroupMember, RosterRow } from './types'

const { overrides } = vi.hoisted(() => ({ overrides: {} as Record<string, unknown> }))

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof HermesSdk>()

  overrides.request = vi.fn()
  overrides.requestProfile = vi.fn()

  return {
    ...sdk,
    host: new Proxy(sdk.host, {
      get: (target, prop) => (prop in overrides ? overrides[prop as string] : Reflect.get(target, prop))
    })
  }
})

const {
  $botMeta,
  botFullMentionTag,
  botMentionTag,
  botMentionTagAmong,
  friendlyNamePart,
  mentionShortForms,
  resolveRosterMentions
} = await import('./data')

const { buildGroupChatTurnPrompt } = await import('./group-round-prompt')
const { groupReplyMentionTag, parseGroupChatMentions } = await import('./group-rounds')

const team: GroupMember[] = [
  { name: 'default', title: 'Neo | Chief of Staff' },
  { name: 'atlas', title: 'Atlas | Growth Lead' },
  { name: 'apex', title: 'Apex | GTM Lead' },
  { name: 'forge', title: 'Forge | Hardware Lead' },
  { name: 'delta', title: 'Delta | Capital Desk' },
  { name: 'kepler', title: 'Kepler | ML & Product' },
  { name: 'spark', title: 'Spark | Talent Lead' }
] as GroupMember[]

const asRows = (members: GroupMember[]) => members as unknown as RosterRow[]

beforeEach(() => {
  $botMeta.set({})
})

describe('the name half of a "Name | Role" title', () => {
  it('splits on a pipe or a spaced dot/dash, and leaves plain titles whole', () => {
    expect(friendlyNamePart('Atlas | Growth Lead')).toBe('Atlas')
    expect(friendlyNamePart('Kepler | ML & Product')).toBe('Kepler')
    expect(friendlyNamePart('Dr Foo · Research')).toBe('Dr Foo')
    expect(friendlyNamePart('Vera — Ops')).toBe('Vera')
    expect(friendlyNamePart('Research Buddy')).toBeNull()
    expect(friendlyNamePart('Re-search')).toBeNull()
    expect(friendlyNamePart('')).toBeNull()
  })

  it('never lets a short name claim a reserved tag', () => {
    expect(mentionShortForms('Hermes | Chief of Staff')).toEqual([])
    expect(botMentionTag({ name: 'ops', title: 'Hermes | Ops' } as GroupMember)).toBe('hermes-ops')
  })
})

describe('the tag a bot is offered under', () => {
  it('is the short name for "Name | Role" titles, the whole slug otherwise', () => {
    expect(team.map(botMentionTag)).toEqual(['neo', 'atlas', 'apex', 'forge', 'delta', 'kepler', 'spark'])
    expect(botFullMentionTag(team[1])).toBe('atlas-growth-lead')
    expect(botMentionTag({ name: 'writer', title: 'Research Buddy' } as GroupMember)).toBe('research-buddy')
  })

  it('falls back to the whole title when another bot claims the short name', () => {
    const twin = { name: 'scout', title: 'Atlas | Research' } as GroupMember

    expect(
      botMentionTagAmong(
        team[1],
        team.filter(m => m !== team[1])
      )
    ).toBe('atlas')
    expect(botMentionTagAmong(team[1], [...team.filter(m => m !== team[1]), twin])).toBe('atlas-growth-lead')
    expect(botMentionTagAmong(twin, team)).toBe('atlas-research')
    // An exact profile name elsewhere wins too.
    expect(botMentionTagAmong({ name: 'scout', title: 'Apex | Scout' } as GroupMember, team)).toBe('apex-scout')
  })
})

describe('resolving short tags', () => {
  it('routes @neo and @atlas, and still accepts the whole-title tags', () => {
    const live = { connectionId: 'local', name: 'spark' }
    const names = (text: string) => resolveRosterMentions(text, asRows(team), live).map(bot => bot.name)

    expect(names('quick one @neo and @atlas')).toEqual(['default', 'atlas'])
    expect(names('@atlas-growth-lead and @apex-gtm-lead')).toEqual(['atlas', 'apex'])
  })

  it('never lets a short name steal an exact name, and drops a shared one', () => {
    const live = { connectionId: 'local', name: 'nobody' }

    const roster = asRows([
      { name: 'atlas' },
      { name: 'scout', title: 'Atlas | Scout' },
      { name: 'one', title: 'Dr Foo | Research' },
      { name: 'two', title: 'Dr Foo | Sales' }
    ] as GroupMember[])

    const names = (text: string) => resolveRosterMentions(text, roster, live).map(bot => bot.name)

    expect(names('@atlas')).toEqual(['atlas'])
    expect(names('@atlas-scout')).toEqual(['scout'])
    expect(names('@dr-foo')).toEqual([])
    expect(names('@dr-foo-sales')).toEqual(['two'])
  })

  it('resolves a multi-word short name in a group room, unless two members share it', () => {
    const solo = [...team, { name: 'one', title: 'Dr Foo | Research' }] as GroupMember[]

    expect([...parseGroupChatMentions('@dr-foo can you check?', solo).mentioned]).toEqual(['one'])

    const shared = [...solo, { name: 'two', title: 'Dr Foo | Sales' }] as GroupMember[]

    expect([...parseGroupChatMentions('@dr-foo can you check?', shared).mentioned]).toEqual([])
    expect([...parseGroupChatMentions('@dr-foo-sales go', shared).mentioned]).toEqual(['two'])
  })

  it('seeds "Reply to" with the short tag, or the whole title when it is shared', () => {
    expect(groupReplyMentionTag(team[1], team)).toBe('atlas')

    const shared = [...team, { name: 'scout', title: 'Atlas | Research' }] as GroupMember[]

    expect(groupReplyMentionTag(shared[1], shared)).toBe('atlas-growth-lead')
  })
})

describe('the group prompt', () => {
  it('introduces every member by its short tag', () => {
    const prompt = buildGroupChatTurnPrompt({ deltaLines: [], groupName: 'Team', members: team, viewer: team[0] })

    expect(prompt).toContain('You are @neo,')
    expect(prompt).toContain('Atlas | Growth Lead (@atlas)')
    expect(prompt).toContain('Apex | GTM Lead (@apex)')
    expect(prompt).not.toMatch(/@\w+-(growth|gtm|hardware)-lead/)
  })

  it('introduces two same-named members by their whole titles', () => {
    const members = [...team, { name: 'scout', title: 'Atlas | Research' }] as GroupMember[]
    const prompt = buildGroupChatTurnPrompt({ deltaLines: [], groupName: 'Team', members, viewer: team[0] })

    expect(prompt).toContain('Atlas | Growth Lead (@atlas-growth-lead)')
    expect(prompt).toContain('Atlas | Research (@atlas-research)')
  })
})
