import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MarkdownTextContent } from './markdown-text'

// Regression for a Slack draft a bot showed for review (Forge, 19 Nov 2026).
// The draft was quoted in raw Slack mrkdwn: every `<url|label>` painted as
// `https://aperdata.ai|AperData [blocked]` (the URL hardening cannot parse a
// host ending in `|AperData`), labels with a space leaked their `<`/`>`, and
// the whole quote rendered as grey italics. The reader must get the labels as
// links, no raw URL or hardening tag, and quoted text that reads as text.
const FORGE_DRAFT = [
  '**Slack draft for #lab-brain:**',
  '',
  "> @Armin @Loïc since you're in China from Sunday, I just intro'd you to 14 hardware providers. I told them you'll be there and that you'll take it from here:",
  "> • *Email, you're in CC:* <https://aperdata.ai|AperData>, <https://bitrobot.ai|BitRobot>, <https://www.skyworth.com|Skyworth>, <https://mutual.solutions|Mutual Solutions>, <https://www.physen.ai|Physen>, <https://www.orbbec.com|Orbbec>, <https://global.agilex.ai|AgileX>, <https://i2rt.com|I2RT>, <https://galaxea-dynamics.com|Galaxea> (via Robotopian), <https://www.xvisiotech.com|Xvisio>, <https://www.unitree.com|Unitree>",
  '> • *Slack:* <https://genrobot.ai|GenRobot> in #ext-genrobot-hub',
  "> • *WhatsApp:* <https://shenzhenfoundry.com|Shenzhen Foundry> and <https://www.orobot.io|Orobot>. I'll add you to a group once they reply",
  '>',
  '> <https://drive.google.com/file/d/1UGIvkLVgPVfOyPwjv58PjORdaLptANeL/view|Hardware priority list for Shenzhen> · <https://drive.google.com/file/d/1Nh-xPZRrDv_FVWlQnMG0SgQx4AluGUeo/view|Robotics events in Shenzhen until end of year>'
].join('\n')

const EXPECTED_LINKS: [string, string][] = [
  ['AperData', 'https://aperdata.ai'],
  ['BitRobot', 'https://bitrobot.ai'],
  ['Skyworth', 'https://www.skyworth.com'],
  ['Mutual Solutions', 'https://mutual.solutions'],
  ['Physen', 'https://www.physen.ai'],
  ['Orbbec', 'https://www.orbbec.com'],
  ['AgileX', 'https://global.agilex.ai'],
  ['I2RT', 'https://i2rt.com'],
  ['Galaxea', 'https://galaxea-dynamics.com'],
  ['Xvisio', 'https://www.xvisiotech.com'],
  ['Unitree', 'https://www.unitree.com'],
  ['GenRobot', 'https://genrobot.ai'],
  ['Shenzhen Foundry', 'https://shenzhenfoundry.com'],
  ['Orobot', 'https://www.orobot.io'],
  ['Hardware priority list for Shenzhen', 'https://drive.google.com/file/d/1UGIvkLVgPVfOyPwjv58PjORdaLptANeL/view'],
  [
    'Robotics events in Shenzhen until end of year',
    'https://drive.google.com/file/d/1Nh-xPZRrDv_FVWlQnMG0SgQx4AluGUeo/view'
  ]
]

describe('a quoted Slack draft in mrkdwn', () => {
  afterEach(cleanup)

  it('paints every Slack link as its label, pointing at its URL', async () => {
    const { container } = render(<MarkdownTextContent isRunning={false} text={FORGE_DRAFT} />)

    await screen.findByText('AperData')
    const anchors = Array.from(container.querySelectorAll('blockquote a')) as HTMLAnchorElement[]

    // The URL hardening rebuilds each href from the parsed URL (`…ai` -> `…ai/`).
    expect(anchors.map(anchor => [anchor.textContent?.trim(), anchor.getAttribute('href')])).toEqual(
      EXPECTED_LINKS.map(([label, href]) => [label, new URL(href).href])
    )
  })

  it('shows no raw URL, pipe, angle bracket or hardening tag', async () => {
    const { container } = render(<MarkdownTextContent isRunning={false} text={FORGE_DRAFT} />)

    await screen.findByText('Mutual Solutions')
    const quote = container.querySelector('blockquote')?.textContent ?? ''

    expect(quote).not.toContain('[blocked]')
    expect(quote).not.toMatch(/https?:\/\//)
    expect(quote).not.toMatch(/[<>|]/)
  })

  it('renders the quoted draft upright, not as italics', async () => {
    const { container } = render(<MarkdownTextContent isRunning={false} text={FORGE_DRAFT} />)

    await screen.findByText('AperData')
    const quote = container.querySelector('blockquote') as HTMLElement

    expect(quote.className.split(/\s+/)).not.toContain('italic')
  })
})
