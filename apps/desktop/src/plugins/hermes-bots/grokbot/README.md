# grokbot (vendored)

Engine for the Grok Bot avatar family, vendored from
[jeremy-prt/bloub](https://github.com/jeremy-prt/bloub) at `b4bb3c1b5f93c7b87a2e8d620f667c4093d97749` (MIT, see LICENSE).
bloub is a frame-measured recreation of xAI's Grok Bot avatar: 8 body shapes,
16 rest expressions, 15 animated states. Its constants are measurements, not
tuning: do not round them.

Local changes: formatting only (eslint --fix + prettier to match this repo).
bloub's own engine/shape/skins/expressions/face tests are kept alongside and
must stay green. The Hermes integration lives one level up in `grok-face.tsx`.
