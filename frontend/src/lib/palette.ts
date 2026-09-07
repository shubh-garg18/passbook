/* Which eight colours the charts use. SPEC §111.
 *
 *   "there should be a color picker choice for user see better practices"
 *
 * **Presets, not a hex picker.** A free-form picker is the obvious build and
 * the wrong one: eight colours have to stay legible on the card, stay distinct
 * from each other, and mean the same thing on every page. A person choosing
 * eight hexes cannot check any of that, and the first illegible chart is one
 * they made themselves and cannot diagnose.
 *
 * So the choice is between palettes that were each derived and measured — hue
 * spacing in OKLCh, contrast against `--sheet`, and closest-pair separation in
 * OKLab — and the numbers travel with them, in `note`, so the choice is
 * informed rather than aesthetic guesswork.
 *
 * The one that matters most is `safe`: Okabe-Ito is the standard set for
 * red-green colour blindness, which about 1 in 12 men has, and no amount of
 * hue-wheel arithmetic substitutes for it.
 */

export type PaletteId = 'bright' | 'deep' | 'wheel' | 'safe'

export type Palette = {
  id: PaletteId
  label: string
  /** What it is for, and the measurement that backs it. */
  note: string
  cats: [string, string, string, string, string, string, string, string]
}

const ALL: Palette[] = [
  {
    id: 'bright',
    label: 'Bright',
    note: 'Vivid, and no purple, pink or yellow. 4.6:1 at worst.',
    // §111. Hues 22-266 — red through blue, skipping the yellow band (80-118)
    // and the magenta band (>282). Lightness solved per hue to maximise the
    // closest pair: 0.130 in OKLab, contrast 4.57 to 9.21 against --sheet.
    cats: ['#ff1f41', '#e47602', '#7ccb05', '#02ad6d',
           '#09d3c4', '#0097ac', '#11afff', '#4c7aff'],
  },
  {
    id: 'deep',
    label: 'Deep',
    note: 'The same hues, muted. Easier on a long page. 3.6:1 at worst.',
    // Fixed chroma 0.15 rather than max — natural rather than neon. Closest
    // pair 0.119, contrast 3.61 to 6.97.
    cats: ['#df6768', '#e48233', '#51850e', '#2eba7a',
           '#048077', '#10b1ca', '#0689c8', '#729afb'],
  },
  {
    id: 'wheel',
    label: 'Full wheel',
    note: 'Eight hues evenly spaced — the most separable, and it includes purple.',
    // A complete 45-degree wheel at L 0.72, max chroma. Separation is better
    // than any restricted set can be, at the cost of the hues that were
    // rejected by name — which is exactly why it is a choice and not the
    // default. Contrast 6.36 at worst.
    cats: ['#76a2fe', '#c87dff', '#ff64ad', '#fe7647',
           '#cf9b0a', '#7fba04', '#08c09e', '#02b7db'],
  },
  {
    id: 'safe',
    label: 'Colour-blind safe',
    note: 'Okabe-Ito, the standard set. Distinct with red-green colour blindness.',
    // The published eight, with one substitution: Okabe-Ito's black measures
    // 1.21:1 against this card and cannot be seen on it, so the neutral slot is
    // a grey at 7.14:1. Everything else is unchanged, because the point of a
    // standard set is that it is the standard set.
    cats: ['#e69f00', '#56b4e9', '#009e73', '#f0e442',
           '#0072b2', '#d55e00', '#cc79a7', '#a6a6a6'],
  },
]

export const PALETTES = ALL
/** The first is the fallback everywhere, so it is named once. */
const FALLBACK = ALL[0] as Palette

const KEY = 'passbook.palette'
export const DEFAULT_PALETTE: PaletteId = 'bright'

export function readPalette(): PaletteId {
  try {
    const stored = window.localStorage.getItem(KEY)
    if (PALETTES.some((p) => p.id === stored)) return stored as PaletteId
  } catch {
    /* private mode throws; the default is always right */
  }
  return DEFAULT_PALETTE
}

/** Stamp the eight tokens onto the root, so every chart follows at once.
 *
 *  Set as CSS variables rather than a class per palette: `catColor()` reads
 *  `--cat-N` off the computed style, the light theme redefines the same eight
 *  names, and a variable is the one mechanism both already use. */
export function applyPalette(id: PaletteId): void {
  const palette = PALETTES.find((p) => p.id === id) ?? FALLBACK
  const root = document.documentElement
  palette.cats.forEach((hex, index) => {
    root.style.setProperty(`--cat-${index + 1}`, hex)
  })
  root.dataset.palette = palette.id
  try {
    window.localStorage.setItem(KEY, palette.id)
  } catch {
    /* see above */
  }
}
