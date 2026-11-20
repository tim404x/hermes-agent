import remarkGfm from 'remark-gfm'
import remarkParse from 'remark-parse'
import { unified } from 'unified'
import { describe, expect, it } from 'vitest'

import { preprocessMarkdown } from './markdown-preprocess'

/**
 * Slack mrkdwn entities reach the transcript whenever a bot quotes a Slack
 * message or shows a Slack draft. None of them is markdown: `<url|label>`
 * parsed as an autolink whose href kept `|label`, the URL hardening could not
 * parse that host and painted `https://aperdata.ai|AperData [blocked]`; a label
 * with a space was not even an autolink, so `<`/`>` leaked around a blocked
 * fragment; `<!here>` is an HTML declaration and vanished. They must read the
 * way Slack shows them: the label as a link, `#channel`, `@name`, `@here`.
 */

interface MdNode {
  children?: MdNode[]
  type: string
  url?: string
  value?: string
}

function parse(markdown: string): MdNode {
  return unified().use(remarkParse).use(remarkGfm).parse(preprocessMarkdown(markdown)) as MdNode
}

function textOf(node: MdNode): string {
  return node.value ?? (node.children ?? []).map(textOf).join('')
}

// Every link the renderer's parser produces, as [href, visible label].
function links(markdown: string): [string, string][] {
  const found: [string, string][] = []

  const walk = (node: MdNode) => {
    if (node.type === 'link') {
      found.push([node.url ?? '', textOf(node)])

      return
    }

    node.children?.forEach(walk)
  }

  walk(parse(markdown))

  return found
}

// The visible text, links included, as the reader sees it.
function visibleText(markdown: string): string {
  return textOf(parse(markdown))
}

describe('Slack mrkdwn entities in prose', () => {
  it('renders a Slack link as its label pointing at the URL', () => {
    expect(links('Email, you are in CC: <https://aperdata.ai|AperData>, <https://bitrobot.ai|BitRobot>')).toEqual([
      ['https://aperdata.ai', 'AperData'],
      ['https://bitrobot.ai', 'BitRobot']
    ])
  })

  it('keeps a label with spaces whole, with no stray brackets', () => {
    const source = '<https://mutual.solutions|Mutual Solutions> and <https://shenzhenfoundry.com|Shenzhen Foundry>'

    expect(links(source)).toEqual([
      ['https://mutual.solutions', 'Mutual Solutions'],
      ['https://shenzhenfoundry.com', 'Shenzhen Foundry']
    ])
    expect(visibleText(source)).toBe('Mutual Solutions and Shenzhen Foundry')
  })

  it('converts links inside a quoted draft and its bullets', () => {
    const draft = [
      '> • *Slack:* <https://genrobot.ai|GenRobot> in #ext-genrobot-hub',
      '>',
      '> <https://drive.google.com/file/d/1UGIvkLVgPVfOyPwjv58PjORdaLptANeL/view|Hardware priority list for Shenzhen> · <https://drive.google.com/file/d/1Nh-xPZRrDv_FVWlQnMG0SgQx4AluGUeo/view|Robotics events in Shenzhen until end of year>'
    ].join('\n')

    expect(links(draft)).toEqual([
      ['https://genrobot.ai', 'GenRobot'],
      ['https://drive.google.com/file/d/1UGIvkLVgPVfOyPwjv58PjORdaLptANeL/view', 'Hardware priority list for Shenzhen'],
      [
        'https://drive.google.com/file/d/1Nh-xPZRrDv_FVWlQnMG0SgQx4AluGUeo/view',
        'Robotics events in Shenzhen until end of year'
      ]
    ])
    expect(visibleText(draft)).not.toMatch(/[<>|]|https?:/)
  })

  it('converts mailto links and keeps a pipe inside the label', () => {
    expect(links('Write to <mailto:tim@hub.xyz|Tim> about <https://x.example/a|Shoot Pipeline HQ | Calendar>')).toEqual(
      [
        ['mailto:tim@hub.xyz', 'Tim'],
        ['https://x.example/a', 'Shoot Pipeline HQ | Calendar']
      ]
    )
  })

  it('keeps brackets and parentheses from breaking the link', () => {
    expect(links('<https://en.wikipedia.org/wiki/Foo_(bar)|Foo [the bar]> then <https://example.com/x)y|odd>')).toEqual(
      [
        ['https://en.wikipedia.org/wiki/Foo_(bar)', 'Foo [the bar]'],
        ['https://example.com/x)y', 'odd']
      ]
    )
  })

  it('shows labeled channel and user references and broadcasts the way Slack does', () => {
    expect(visibleText('<!here> <@U049G97J56F|armin> see <#C0C48HB1NFM|sales-b2b> and <!channel>')).toBe(
      '@here @armin see #sales-b2b and @channel'
    )
  })

  it('leaves Slack syntax inside code untouched', () => {
    const source = 'Payload: `<https://aperdata.ai|AperData>`\n\n```\n<https://bitrobot.ai|BitRobot> <!here>\n```'

    expect(links(source)).toEqual([])
    expect(preprocessMarkdown(source)).toContain('`<https://aperdata.ai|AperData>`')
    expect(preprocessMarkdown(source)).toContain('<https://bitrobot.ai|BitRobot> <!here>')
  })

  it('leaves plain autolinks and markdown links as they were', () => {
    expect(links('See <https://hub.xyz> and [Hub](https://hub.xyz/about)')).toEqual([
      ['https://hub.xyz', 'https://hub.xyz'],
      ['https://hub.xyz/about', 'Hub']
    ])
  })
})
