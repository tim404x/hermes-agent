import { clamp, createRng, loopNoise } from './math'

/**
 * Les yeux sont peints sur une sphere, pas poses a plat.
 *
 * Mesure sur la video : l'oeil le plus proche du bord fait 0.69 fois la largeur
 * de l'autre, et son aire 0.663 fois — exactement le facteur de profondeur
 * (z = 0.669) d'un point de sphere a cette distance du centre. On modelise donc
 * une vraie orientation de tete : chaque oeil recupere le repere tangent de la
 * sphere, projete en orthographique. La compression et l'inclinaison en
 * decoulent toutes seules, c'est ce qui donne le volume.
 *
 * Les constantes ci-dessous ne sont pas choisies a la main : elles sortent d'un
 * ajustement du modele sur les positions et tailles relevees image par image
 * (erreur residuelle ~1 px sur un rayon de 190 px).
 */

export type Vec3 = [number, number, number]

/** Demi-ecart des yeux sur la sphere, en degres (separation totale ~31deg). */
export const EYE_SPLIT = 15.46
/** Taille de l'oeil au repos, en unites de rayon de boule. */
export const EYE_W = 0.186
export const EYE_H = 0.412
/**
 * Hermes: rendered eye size multiplier on top of bloub's measured rest size.
 * Tim's reference render of xAI's bot (18 Nov 2026) has eyes 1.4x bloub's in
 * length and width; applied at render time so every expression scales alike.
 */
export const EYE_SCALE = 1.4

/** Orientation de tete au repos, ajustee sur les frames de reference. */
// Hermes: re-fitted on Tim's xAI reference render (18 Nov 2026), with EYE_SCALE and the
// spherical eye wrap: the eyes sit lower, nearer the middle and more tilted than bloub's
// video fit ({ yaw: 28.49, pitch: 28.62, roll: -13 }).
export const REST_GAZE: HeadGaze = { yaw: 18, pitch: 26, roll: -27 }
/** bloub's rest gaze, as fitted on the reference video. Kept: the sphere-model tests check against it. */
export const VIDEO_REST_GAZE: HeadGaze = { yaw: 28.49, pitch: 28.62, roll: -13 }

export interface EyePose {
  x: number
  y: number
  /** matrice tangente 2x2 : [a b c d] au sens SVG matrix(a,b,c,d,e,f) */
  a: number
  b: number
  c: number
  d: number
  /** composante z de la normale : > 0 = face visible */
  depth: number
  /** Hermes: 3D frame of the eye (normal, tangent right, tangent down) for the spherical wrap */
  normal: Vec3
  tangentX: Vec3
  tangentY: Vec3
}

export interface HeadGaze {
  /** lacet, degres, positif = regarde a droite */
  yaw: number
  /** tangage, degres, positif = regarde en haut */
  pitch: number
  /** roulis, degres, inclinaison de la tete */
  roll: number
}

const deg = (d: number) => (d * Math.PI) / 180

/** Fait tourner deux vecteurs d'un repere orthonorme dans leur plan commun. */
function spin(u: Vec3, v: Vec3, angle: number): [Vec3, Vec3] {
  const c = Math.cos(angle)
  const s = Math.sin(angle)

  return [
    [u[0] * c + v[0] * s, u[1] * c + v[1] * s, u[2] * c + v[2] * s],
    [v[0] * c - u[0] * s, v[1] * c - u[1] * s, v[2] * c - u[2] * s]
  ]
}

/**
 * Repere de la tete puis des deux yeux.
 * Repere ecran : x a droite, y vers le bas, z vers le spectateur.
 * L'indice 0 est l'oeil interieur, l'indice 1 l'oeil exterieur.
 */
export function eyePoses(gaze: HeadGaze, scale: number, split = EYE_SPLIT): [EyePose, EyePose] {
  let f: Vec3 = [0, 0, 1]
  let right: Vec3 = [1, 0, 0]

  let down: Vec3 = [0, 1, 0]

  // lacet : forward bascule vers right
  ;[f, right] = spin(f, right, deg(gaze.yaw))
  // tangage : forward bascule vers le haut (donc a l'oppose de down)
  ;[down, f] = spin(down, f, deg(gaze.pitch))
  // roulis : la tete penche dans son propre plan
  ;[right, down] = spin(right, down, deg(gaze.roll))

  const build = (side: number): EyePose => {
    const [ef, er] = spin(f, right, deg(split * side))

    return {
      x: ef[0] * scale,
      y: ef[1] * scale,
      a: er[0],
      b: er[1],
      c: down[0],
      d: down[1],
      depth: ef[2],
      normal: ef,
      tangentX: er,
      tangentY: down
    }
  }

  return [build(-1), build(1)]
}

/**
 * Vie au repos : derive lente du regard, saccades, clignements.
 *
 * Fonction pure du temps (aucun etat interne), donc pause, reprise et saut a
 * une date arbitraire donnent toujours la meme image. Les valeurs sont des
 * ECARTS a ajouter a la pose de l'etat courant.
 */
export interface Liveliness {
  dYaw: number
  dPitch: number
  dRoll: number
  /** 1 = oeil ouvert, 0 = ferme (ecrasement vertical en repere ecran) */
  lid: number
  driftX: number
  driftY: number
  breath: number
}

const BLINK_RNG = createRng(0x5eed)

/** Calendrier de clignements pre-tire : deterministe et sans etat. */
const BLINKS: number[] = (() => {
  const out: number[] = []
  let t = 1.4

  while (t < 900) {
    out.push(t)
    // 1.9 a 4.6 s entre deux clignements, plus un double clignement parfois
    t += 1.9 + BLINK_RNG() * 2.7

    if (BLINK_RNG() < 0.18) {
      out.push(t)
      t += 0.24
    }
  }

  return out
})()

/** Mesure : 1 a 2 frames a 10 fps. */
const BLINK_DUR = 0.18

function blinkLid(t: number): number {
  for (let i = 0; i < BLINKS.length; i++) {
    const start = BLINKS[i]!

    if (t < start) {
      break
    }

    const k = (t - start) / BLINK_DUR

    if (k >= 0 && k <= 1) {
      // fermeture rapide, reouverture un peu plus lente
      return k < 0.45 ? 1 - k / 0.45 : (k - 0.45) / 0.55
    }
  }

  return 1
}

export interface LivelinessOptions {
  wander?: number
  blink?: boolean
  float?: boolean
}

export function liveliness(t: number, opt: LivelinessOptions = {}): Liveliness {
  const { wander = 1, blink = true, float = true } = opt

  // Periodes premieres entre elles : la derive ne se repete jamais a l'oeil.
  return {
    dYaw: (loopNoise(t, 11.3, 0.4) * 5.5 + loopNoise(t, 3.7, 2.1) * 1.6) * wander,
    dPitch: (loopNoise(t, 9.1, 1.3) * 4.2 + loopNoise(t, 4.3, 0.7) * 1.3) * wander,
    dRoll: loopNoise(t, 13.7, 3.2) * 2.2 * wander,
    lid: blink ? blinkLid(t) : 1,
    // Au repos la video est quasiment immobile (centre stable a +-0.003, rayon
    // constant) : toute la vie passe par le regard et les clignements. On garde
    // juste de quoi ne pas figer completement l'image.
    driftX: float ? loopNoise(t, 7.9, 1.9) * 0.006 : 0,
    driftY: float ? loopNoise(t, 5.3, 0.3) * 0.007 : 0,
    // La largeur est constante, seule la hauteur respire tres legerement.
    breath: float ? 1 + Math.sin((t / 3.4) * Math.PI * 2) * 0.005 : 1
  }
}

/**
 * Le clignement est un ecrasement VERTICAL en repere ecran autour du centre de
 * l'oeil (mesure : la largeur de bbox est conservee, la hauteur tombe a ~0.35),
 * pas un retrecissement le long de l'axe incline de la gelule. On le compose
 * donc apres la matrice tangente, en n'affectant que les sorties en y.
 */
export function blinkScale(lid: number): number {
  return 0.06 + 0.94 * clamp(lid)
}

/**
 * Hermes: the eye painted ON the sphere instead of a flat capsule under an affine
 * tangent map. The capsule outline (w x h, in sphere-radius units, tilted by `tilt`
 * radians in the eye plane) is sampled in the tangent plane, carried onto the unit
 * sphere along great circles (exponential map) and projected orthographically. The
 * eye nearest the rim therefore bends and thins toward the edge, like xAI's render.
 * `at` places the result: eye centre on screen, sphere radius in px, blink squash.
 */
export function sphereEyePath(
  pose: EyePose,
  tilt: number,
  w: number,
  h: number,
  at: { scale: number; squash: number; x: number; y: number },
  steps = 16
): string {
  const hw = Math.max(w, 0.0001) / 2
  const hh = Math.max(h, 0.0001) / 2
  const r = Math.min(hw, hh)
  const cos = Math.cos(tilt)
  const sin = Math.sin(tilt)
  const ex = pose.tangentX.map((v, i) => v * cos + pose.tangentY[i]! * sin)
  const ey = pose.tangentX.map((v, i) => -v * sin + pose.tangentY[i]! * cos)
  const n = pose.normal
  const pts: string[] = []

  const put = (u: number, v: number) => {
    const t = [u * ex[0]! + v * ey[0]!, u * ex[1]! + v * ey[1]!, u * ex[2]! + v * ey[2]!]
    const len = Math.hypot(t[0]!, t[1]!, t[2]!)
    const c = Math.cos(len)
    const s = len > 1e-9 ? Math.sin(len) / len : 1
    const px = (n[0] * c + t[0]! * s - n[0]) * at.scale
    const py = (n[1] * c + t[1]! * s - n[1]) * at.scale * at.squash
    pts.push(`${Math.round((at.x + px) * 100) / 100} ${Math.round((at.y + py) * 100) / 100}`)
  }

  // Long axis along the eye's own vertical (as capsulePath with h > w): straight
  // sides are sampled too, so they follow the sphere's curvature.
  const capsule = (cx: number, cy: number, from: number) => {
    for (let j = 0; j <= steps; j++) {
      const a = from + (Math.PI * j) / steps
      put(cx + r * Math.cos(a), cy + r * Math.sin(a))
    }
  }

  const side = (x0: number, y0: number, x1: number, y1: number) => {
    for (let j = 1; j < steps; j++) {
      put(x0 + ((x1 - x0) * j) / steps, y0 + ((y1 - y0) * j) / steps)
    }
  }

  if (hh >= hw) {
    capsule(0, -(hh - r), Math.PI)
    side(r, -(hh - r), r, hh - r)
    capsule(0, hh - r, 0)
    side(-r, hh - r, -r, -(hh - r))
  } else {
    capsule(hw - r, 0, -Math.PI / 2)
    side(hw - r, r, -(hw - r), r)
    capsule(-(hw - r), 0, Math.PI / 2)
    side(-(hw - r), -r, hw - r, -r)
  }

  return `M${pts.join('L')}Z`
}

/**
 * Hermes: where the two eyes land, and how big they are drawn.
 *
 * With `face` (one of xAI's bodies, `FaceFit` in skins.ts) the eyes follow xAI's own
 * rule: the round-body centres are scaled by `face.k` about the body centre and moved
 * down by `face.y` radii; the eyes are drawn `face.size` times. Without it (an animated
 * silhouette) bloub's radial fit stays, with one change: the pair's midpoint is fitted,
 * each eye keeps its offset, so the bigger EYE_SCALE eyes never collide on narrow bodies.
 * `scale` is the body radius in output units; `radiusAt(x, y)` the body's radius ratio.
 */
export function placeEyes(
  poses: [EyePose, EyePose],
  scale: number,
  radiusAt: (x: number, y: number) => number,
  face?: { k: number; size: number; y: number } | null
): { at: [{ x: number; y: number }, { x: number; y: number }]; size: number } {
  if (face) {
    return {
      at: [
        { x: poses[0].x * face.k, y: poses[0].y * face.k + face.y * scale },
        { x: poses[1].x * face.k, y: poses[1].y * face.k + face.y * scale }
      ],
      size: face.size
    }
  }

  const mx = (poses[0].x + poses[1].x) / 2
  const my = (poses[0].y + poses[1].y) / 2
  const fit = radiusAt(mx, my)

  return {
    at: [
      { x: mx * fit + (poses[0].x - mx), y: my * fit + (poses[0].y - my) },
      { x: mx * fit + (poses[1].x - mx), y: my * fit + (poses[1].y - my) }
    ],
    size: 1
  }
}
