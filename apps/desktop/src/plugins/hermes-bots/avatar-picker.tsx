/**
 * The avatar editor shared by Edit Profile and New Bot: shape grid + color
 * swatches, the Generate and Upload tabs, and the petdex Pet tab.
 */

import {
  Button,
  cn,
  Codicon,
  ColorSwatches,
  GlyphSpinner,
  host,
  PROFILE_SWATCHES,
  RowButton,
  SegmentedControl,
  Textarea,
  Tip,
  useValue
} from '@hermes/plugin-sdk'
import { useState } from 'react'

import {
  AVATAR_PICKER_SHAPES,
  avatarColor,
  BLOB_KINDS,
  blobatarSvg,
  blobShapeString,
  BotFace,
  defaultShapeFor,
  isBlobShape,
  parseBlobShape
} from './avatar'
import {
  $imagenAvailable,
  generateAvatarImage,
  type GeneratedImage,
  normalizeAvatarImage,
  pickImageFromDevice,
  probeImagen
} from './avatar-image'
import {
  formatGrokShape,
  GROK_EXPRESSIONS,
  GROK_PALETTE,
  GROK_SHAPES,
  type GrokExpressionId,
  type GrokShapeId,
  isGrokShape,
  parseGrokShape
} from './grok-face'
import { useBots } from './i18n'
import { PetTab } from './pet'

interface AvatarPickerProps {
  /** `null` = no explicit pick, i.e. the name's deterministic hue. */
  color: null | string
  /** Feeds the Generate tab when the user leaves the description blank. */
  generateSeed?: { description?: string; name?: string; title?: string } | null
  image: null | string
  onColor: (color: null | string) => void
  onImage: (image: null | string) => void
  onShape: (shape: string) => void
  shape: string
}

/** Shape grid + color swatches, shared by Edit Profile and New Bot. */
export function AvatarPicker({ shape, color, image, onShape, onColor, onImage, generateSeed }: AvatarPickerProps) {
  const b = useBots()
  const pickerName = generateSeed?.name || 'agent'
  const imagen = useValue($imagenAvailable)
  // A Grok Bot face opens on its own tab, so editing it starts where it lives.
  const [tab, setTab] = useState(isGrokShape(shape) ? 'grok' : 'bot')
  const [describe, setDescribe] = useState('')
  const [genBusy, setGenBusy] = useState(false)

  if (imagen === null) {
    void probeImagen()
  }

  // Re-check a stale "unavailable" whenever the user lands on the Generate
  // tab — the gateway may have restarted with image.generate since.
  const goTab = (id: string) => {
    setTab(id)

    if (id === 'generate' && $imagenAvailable.get() === false) {
      $imagenAvailable.set(null)
      void probeImagen()
    }
  }

  const upload = async () => {
    const raw = await pickImageFromDevice()

    if (raw) {
      onImage(await normalizeAvatarImage(raw))
    }
  }

  const generate = async () => {
    if (genBusy) {
      return
    }

    setGenBusy(true)

    try {
      const custom = describe.trim()

      const img = custom
        ? await (async () => {
            const res = await host.request<GeneratedImage>('image.generate', {
              prompt: `${custom}. Avatar for an AI agent: centered, bold flat vector style, solid color background, no text.`,
              aspect_ratio: 'square'
            })

            if (!res?.success) {
              throw new Error(res?.error || 'generation failed')
            }

            return res.image_data || res.image
          })()
        : await generateAvatarImage(generateSeed?.name || 'agent', generateSeed?.title, generateSeed?.description)

      if (img) {
        onImage(await normalizeAvatarImage(img))
      }
    } catch (err) {
      host.notifyError(err, b.avatar.generationFailed)
    } finally {
      setGenBusy(false)
    }
  }

  return (
    <div className="grid justify-items-center gap-3">
      <SegmentedControl
        onChange={goTab}
        options={[
          { id: 'bot', label: b.avatar.tabBot },
          { id: 'grok', label: b.avatar.tabGrok },
          { id: 'generate', label: b.avatar.tabGenerate },
          { id: 'upload', label: b.avatar.upload },
          { id: 'pet', label: b.avatar.tabPet }
        ]}
        value={tab}
      />
      {image && tab !== 'generate' ? (
        <Button onClick={() => onImage(null)} size="sm" type="button" variant="ghost">
          {b.avatar.removeImage}
        </Button>
      ) : null}
      {tab === 'bot' ? (
        isBlobShape(shape) && blobatarSvg ? (
          (() => {
            const { seedPart, kind } = parseBlobShape(shape, pickerName)
            const locked = Boolean(seedPart)

            return (
              <div className="grid justify-items-center gap-3">
                {/* Silhouette pins: Auto (name decides) + the six blob kinds. */}
                <div className="grid grid-cols-4 justify-items-center gap-1.5">
                  {['', ...BLOB_KINDS].map(k => (
                    <Tip key={k || 'auto'} label={k || b.editor.autoHint}>
                      <RowButton
                        aria-label={k || b.editor.autoHint}
                        className={cn(
                          'flex size-11 items-center justify-center rounded-md transition-colors hover:bg-(--chrome-action-hover)',
                          k === kind && !image && 'ring-1 ring-(--ui-accent)'
                        )}
                        onClick={() => {
                          onImage(null)
                          onShape(blobShapeString(seedPart, k))
                        }}
                      >
                        {k ? (
                          <BotFace
                            color={avatarColor(color, pickerName)}
                            name={pickerName}
                            shape={blobShapeString(seedPart, k)}
                            size={32}
                          />
                        ) : (
                          <span className="text-[0.6rem] text-(--ui-text-tertiary)">{b.editor.auto}</span>
                        )}
                      </RowButton>
                    </Tip>
                  ))}
                </div>
                <div className="flex items-center gap-1">
                  <Button
                    onClick={() => {
                      onImage(null)
                      onShape(blobShapeString(Math.random().toString(36).slice(2, 10), kind))
                    }}
                    size="sm"
                    type="button"
                    variant="ghost"
                  >
                    <Codicon className="mr-1 text-[0.8rem]" name="refresh" />
                    {b.avatar.randomize}
                  </Button>
                  <Button
                    onClick={() => onShape(blobShapeString(locked ? '' : pickerName, kind))}
                    size="sm"
                    type="button"
                    variant="ghost"
                  >
                    <Codicon className="mr-1 text-[0.8rem]" name={locked ? 'unlock' : 'lock'} />
                    {locked ? b.editor.unlock : b.editor.lockFace}
                  </Button>
                </div>
                <div className="text-center text-[0.65rem] text-(--ui-text-quaternary)">
                  {locked ? b.editor.lockedHint : b.editor.unlockedHint}
                </div>
                <Button
                  className="text-(--ui-text-tertiary)"
                  onClick={() => onShape(defaultShapeFor(pickerName))}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  {b.avatar.classicShapes}
                </Button>
              </div>
            )
          })()
        ) : (
          <div className="grid justify-items-center gap-3">
            <div className="grid grid-cols-4 justify-items-center gap-1.5">
              {(blobatarSvg ? ['blobatar', ...AVATAR_PICKER_SHAPES] : AVATAR_PICKER_SHAPES).map(s => (
                <Tip key={s} label={s === 'blobatar' ? b.avatar.blobFromName : undefined}>
                  <RowButton
                    aria-label={s === 'blobatar' ? b.avatar.blobFromName : s}
                    className={cn(
                      'flex size-11 items-center justify-center rounded-md transition-colors hover:bg-(--chrome-action-hover)',
                      s === shape && !image && 'ring-1 ring-(--ui-accent)'
                    )}
                    onClick={() => {
                      onImage(null)
                      onShape(s)
                    }}
                  >
                    <BotFace color={avatarColor(color, pickerName)} name={pickerName} shape={s} size={32} />
                  </RowButton>
                </Tip>
              ))}
            </div>
            <ColorSwatches
              clearLabel={b.avatar.matchTheName}
              onChange={onColor}
              swatches={PROFILE_SWATCHES}
              value={color}
            />
          </div>
        )
      ) : null}
      {tab === 'grok' ? (
        <GrokTab
          color={color}
          customLabel={b.avatar.customColor}
          image={image}
          matchLabel={b.avatar.matchTheName}
          name={pickerName}
          onColor={onColor}
          onImage={onImage}
          onShape={onShape}
          shape={shape}
        />
      ) : null}
      {tab === 'generate' ? (
        imagen ? (
          <div className="grid w-full gap-2">
            <Textarea
              className="min-h-16 text-xs"
              onChange={event => setDescribe(event.target.value)}
              placeholder={b.avatar.describePlaceholder}
              value={describe}
            />
            <Button
              className="w-full justify-center"
              disabled={genBusy}
              onClick={generate}
              type="button"
              variant="secondary"
            >
              {genBusy ? (
                <GlyphSpinner className="mr-1 text-[0.8rem]" spinner="breathe" />
              ) : (
                <Codicon className="mr-1 text-[0.8rem]" name="sparkle" />
              )}
              {genBusy ? b.avatar.generating : b.avatar.generate}
            </Button>
            {describe.trim() ? null : (
              <div className="text-center text-[0.65rem] text-(--ui-text-quaternary)">{b.bot.descriptionHint}</div>
            )}
          </div>
        ) : (
          <div className="px-2 py-3 text-center text-xs leading-5 text-(--ui-text-tertiary)">
            {imagen === false ? b.editor.noImageModel : b.editor.checkingImage}
          </div>
        )
      ) : null}
      {tab === 'upload' ? (
        <Button className="w-full justify-center" onClick={upload} type="button" variant="secondary">
          <Codicon className="mr-1 text-[0.8rem]" name="device-camera" />
          {b.editor.chooseImage}
        </Button>
      ) : null}
      {tab === 'pet' ? <PetTab image={image} onImage={onImage} /> : null}
    </div>
  )
}

const GROK_SHAPE_IDS = Object.keys(GROK_SHAPES) as GrokShapeId[]
const GROK_EXPRESSION_IDS = Object.keys(GROK_EXPRESSIONS) as GrokExpressionId[]

/** `<input type="color">` only speaks #rrggbb; anything else starts it on the xAI violet. */
function colorInputValue(color: string) {
  return /^#[0-9a-f]{6}$/i.test(color) ? color : '#8b5cf6'
}

interface GrokTabProps {
  color: null | string
  customLabel: string
  image: null | string
  matchLabel: string
  name: string
  onColor: (color: null | string) => void
  onImage: (image: null | string) => void
  onShape: (shape: string) => void
  shape: string
}

/** Grok Bot faces: 8 bodies, 16 rest expressions, any colour (xAI palette or custom). */
function GrokTab({ color, customLabel, image, matchLabel, name, onColor, onImage, onShape, shape }: GrokTabProps) {
  const current = parseGrokShape(shape)
  const body = current?.shape ?? 'circle'
  const mood = current?.expression ?? 'neutral'
  const ink = avatarColor(color, name)

  const pick = (next: string) => {
    onImage(null)
    onShape(next)
  }

  return (
    <div className="grid justify-items-center gap-3">
      <div className="grid grid-cols-4 justify-items-center gap-1.5">
        {GROK_SHAPE_IDS.map(id => (
          <Tip key={id} label={id}>
            <RowButton
              aria-label={id}
              className={cn(
                'flex size-11 items-center justify-center rounded-md transition-colors hover:bg-(--chrome-action-hover)',
                current?.shape === id && !image && 'ring-1 ring-(--ui-accent)'
              )}
              onClick={() => pick(formatGrokShape(id, mood))}
            >
              <BotFace color={ink} name={name} shape={formatGrokShape(id, mood)} size={32} />
            </RowButton>
          </Tip>
        ))}
      </div>
      <div className="grid grid-cols-8 justify-items-center gap-1">
        {GROK_EXPRESSION_IDS.map(id => (
          <Tip key={id} label={id}>
            <RowButton
              aria-label={id}
              className={cn(
                'flex size-8 items-center justify-center rounded-md transition-colors hover:bg-(--chrome-action-hover)',
                current && current.expression === id && !image && 'ring-1 ring-(--ui-accent)'
              )}
              onClick={() => pick(formatGrokShape(body, id))}
            >
              <BotFace color={ink} name={name} shape={formatGrokShape(body, id)} size={24} />
            </RowButton>
          </Tip>
        ))}
      </div>
      <ColorSwatches clearLabel={matchLabel} onChange={onColor} swatches={GROK_PALETTE} value={color} />
      <label className="flex items-center gap-2 text-[0.65rem] text-(--ui-text-tertiary)">
        {customLabel}
        <input
          aria-label={customLabel}
          className="h-5 w-8 cursor-pointer rounded border-0 bg-transparent p-0"
          onChange={event => onColor(event.target.value)}
          type="color"
          value={colorInputValue(ink)}
        />
      </label>
    </div>
  )
}
