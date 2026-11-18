import { PROFILE_SAMPLES } from './profiles'
import { XAI_PROFILE_RAYS, XAI_PROFILES } from './xai-profiles'

/**
 * Formes et couleurs proposees par le personnalisateur du bot.
 *
 * A la difference des silhouettes d'animation (`profiles.ts`), celles-ci ne sont
 * PAS relevees sur la video. Hermes : ce sont les contours exacts des avatars de
 * xAI (`xai-profiles.ts`), plus des constructions analytiques.
 */

/**
 * Les identifiants sont enumeres plutot que deduits du tableau : c'est ce qui
 * permet a la couche i18n de verifier A LA COMPILATION que chaque forme a bien
 * sa traduction dans les trois langues (`t(\`shapes.${id}\`)` ne compile que si
 * la cle existe). Un `as const` sur le tableau aurait le meme effet mais
 * rendrait `radii` en lecture seule, alors que le moteur le passe tel quel.
 */
export type ShapeId = 'cercle' | 'galet' | 'squircle' | 'capsule' | 'triangle' | 'hexagone' | 'nuage' | 'goutte'

/**
 * Hermes: xAI's face placement for a body. Eye centres (as laid out on the round
 * body) are scaled by `k` about the body centre then moved down by `y` body radii;
 * each eye is drawn `size` times its round-body size. Read off the eye transforms
 * of xAI's eight design-post avatars (residual under 0.6 px on a 140 px avatar).
 */
export interface FaceFit {
  k: number
  size: number
  y: number
}

export interface BotShape {
  id: ShapeId
  radii: number[]
  face?: FaceFit
}

/** Hermes: xAI's exact outline, resampled from its 256 rays to PROFILE_SAMPLES. */
function xai(id: keyof typeof XAI_PROFILES): number[] {
  const src = XAI_PROFILES[id]

  return Array.from({ length: PROFILE_SAMPLES }, (_, i) => {
    const t = (i / PROFILE_SAMPLES) * XAI_PROFILE_RAYS
    const j = Math.floor(t)
    const a = src[j % XAI_PROFILE_RAYS] ?? 1
    const b = src[(j + 1) % XAI_PROFILE_RAYS] ?? 1

    return a + (b - a) * (t - j)
  })
}

// Hermes (18 Nov 2026): the seven non-circle bodies are xAI's own outlines, traced off
// the design post's avatars (`xai-profiles.ts`). They replace bloub's approximations
// (harmonic pebble, five-disc cloud, disc-hull droplet and capsule, rounded polygons):
// Tim wanted pixel-exact Grok shapes.

export const SHAPES: BotShape[] = [
  { id: 'cercle', radii: new Array(PROFILE_SAMPLES).fill(1), face: { k: 1, size: 1, y: 0 } },
  { id: 'galet', radii: xai('galet'), face: { k: 0.966, size: 1.0, y: 0.045 } },
  { id: 'squircle', radii: xai('squircle'), face: { k: 0.995, size: 1.0, y: -0.001 } },
  { id: 'capsule', radii: xai('capsule'), face: { k: 0.929, size: 0.82, y: 0.124 } },
  { id: 'triangle', radii: xai('triangle'), face: { k: 0.7, size: 0.79, y: 0.207 } },
  { id: 'hexagone', radii: xai('hexagone'), face: { k: 0.91, size: 1.0, y: -0.004 } },
  { id: 'nuage', radii: xai('nuage'), face: { k: 0.787, size: 0.818, y: 0.07 } },
  { id: 'goutte', radii: xai('goutte'), face: { k: 0.786, size: 0.887, y: 0.192 } }
]

/** Hermes: face placement by body profile (keyed by the SHAPES radii arrays, by identity). */
export const FACE_BY_RADII = new Map<number[], FaceFit>(
  SHAPES.flatMap(s => (s.face ? [[s.radii, s.face] as const] : []))
)

// Map indexee par `string` et non par `ShapeId` : les appelants interrogent avec
// une valeur relue du localStorage ou d'une prop, donc non validee.
export const SHAPE_BY_ID = new Map<string, BotShape>(SHAPES.map(s => [s.id, s]))
export const DEFAULT_SHAPE = 'cercle'

export type ColorId =
  'encre' | 'creme' | 'brun' | 'rouge' | 'orange' | 'ambre' | 'vert' | 'turquoise' | 'bleu' | 'violet' | 'rose' | 'gris'

export interface BotColor {
  id: ColorId
  hex: string
}

/** Palette du personnalisateur d'origine. Hermes : rouge, orange, ambre, vert, bleu et violet
 *  remplaces par les valeurs officielles de xAI (article de design + table du client). */
export const COLORS: BotColor[] = [
  { id: 'encre', hex: '#0a0a0c' },
  { id: 'brun', hex: '#97683d' },
  { id: 'rouge', hex: '#ff263c' },
  { id: 'orange', hex: '#ff6700' },
  { id: 'ambre', hex: '#ff9800' },
  { id: 'vert', hex: '#00c972' },
  { id: 'turquoise', hex: '#00bca6' },
  { id: 'bleu', hex: '#1084fe' },
  { id: 'violet', hex: '#9159fe' },
  { id: 'rose', hex: '#ff309b' },
  { id: 'gris', hex: '#a3a3a3' },
  { id: 'creme', hex: '#f1efe9' }
]

export const COLOR_BY_ID = new Map<string, BotColor>(COLORS.map(c => [c.id, c]))
export const DEFAULT_COLOR = 'encre'

/** Melange deux couleurs hex. Sert a la brume de profondeur des particules. */
export function mixHex(from: string, to: string, t: number): string {
  const parse = (h: string) => {
    const v = parseInt(h.slice(1), 16)

    return [(v >> 16) & 255, (v >> 8) & 255, v & 255]
  }

  const a = parse(from)
  const b = parse(to)
  const c = a.map((x, i) => Math.round(x + (b[i]! - x) * t))

  return `#${c.map(x => x.toString(16).padStart(2, '0')).join('')}`
}
