# grokbot (vendored)

Engine for the Grok Bot avatar family, vendored from
[jeremy-prt/bloub](https://github.com/jeremy-prt/bloub) at `b4bb3c1b5f93c7b87a2e8d620f667c4093d97749` (MIT, see LICENSE).
bloub is a frame-measured recreation of xAI's Grok Bot avatar: 8 body shapes,
16 rest expressions, 15 animated states. Its constants are measurements, not
tuning: do not round them.

Local changes: formatting (eslint --fix + prettier to match this repo), plus two
measured corrections in `skins.ts` against xAI's own design-post render (16 Nov 2026):
the hexagon is turned point-up (rotation 30, was 0) and six palette colours use
xAI's official values (red, orange, amber, green, blue, violet).
Hermes changes on 18 Nov 2026 (Tim's reference render of xAI's bot): teal, pink and brown
set to the design post's values; eyes rendered 1.4x (`EYE_SCALE`) and wrapped onto the
sphere (`sphereEyePath`, curved and thinner near the rim); `REST_GAZE` re-fitted to match.
bloub's own engine/shape/skins/expressions/face tests are kept alongside and
must stay green. The Hermes integration lives one level up in `grok-face.tsx`.
