/**
 * score.js — the five-band score→color→label mapping.
 *
 * THIS IS THE SINGLE SOURCE OF TRUTH FOR COLOR IN THE APP.
 *
 * BeachCard, Speedometer, BeachMap and BeachDetail all import from here. If you
 * ever find yourself writing `score > 8 ? 'green' : ...` in a component, that's
 * the bug this file exists to prevent — the map markers and the gauges drifting
 * out of sync is exactly what happened in the first cut of this dashboard.
 *
 *   [0,  5)   Poor        Red
 *   [5,  6)   Fair        Orange
 *   [6,  8)   Good        Amber
 *   [8,  9)   Very Good   Green
 *   [9, 10]   Excellent   Blue
 *
 * Bands are HALF-OPEN, lower-bound-inclusive: a score of exactly 6.0 is
 * "Good", 5.9 is "Fair". The only closed end is the very top, so a perfect
 * 10.0 lands in Excellent rather than falling off the table.
 *
 * `min`/`max` also drive geometry — the gauge arc segments and the detail
 * page's band tracks are generated from these numbers, so the widths stay in
 * step with the labels automatically. Note the bands are NOT equal width
 * (Good spans 2.0 while Fair spans 1.0); anything rendering them must use
 * max - min rather than assuming a uniform fifth.
 */

export const SCORE_BANDS = [
  { min: 0, max: 5, label: 'Poor', color: '#C0392B', ink: '#FFFFFF' },
  { min: 5, max: 6, label: 'Fair', color: '#D97706', ink: '#FFFFFF' },
  { min: 6, max: 8, label: 'Good', color: '#E3B008', ink: '#231B02' },
  { min: 8, max: 9, label: 'Very Good', color: '#3F8F5B', ink: '#FFFFFF' },
  { min: 9, max: 10, label: 'Excellent', color: '#2563A8', ink: '#FFFFFF' },
]

export const SCORE_MIN = 0
export const SCORE_MAX = 10

/** Neutral treatment for a beach whose index couldn't be computed. */
export const UNKNOWN_BAND = {
  min: 0,
  max: 10,
  label: 'No data',
  color: '#9A958A',
  ink: '#FFFFFF',
}

/** The band object a score falls into. Null/undefined → UNKNOWN_BAND. */
export function scoreToBand(score) {
  if (score === null || score === undefined || Number.isNaN(Number(score))) {
    return UNKNOWN_BAND
  }
  const s = clampScore(Number(score))
  // Half-open [min, max): the first band whose max the score is strictly under.
  // A perfect 10 matches nothing (10 < 10 is false) and falls through to the
  // final band, which is the intended closed top end.
  return SCORE_BANDS.find((b) => s < b.max) ?? SCORE_BANDS[SCORE_BANDS.length - 1]
}

/** "Very Good" */
export function scoreToLabel(score) {
  return scoreToBand(score).label
}

/** "#3F8F5B" — the fill used for gauge arcs, map markers and detail bars. */
export function scoreToColor(score) {
  return scoreToBand(score).color
}

/**
 * "6.0–7.9" — a band's range, written for a legend.
 *
 * Rendering a half-open [6, 8) band as "6–8" would be actively misleading:
 * an 8.0 reads as Good when it's actually Very Good. Because scores are always
 * displayed to one decimal (see formatScore), spelling the top end as the
 * largest value that really lands in the band is both honest and unambiguous.
 */
export function bandRangeLabel(band) {
  if (band.max >= SCORE_MAX) return `${band.min.toFixed(1)}–${SCORE_MAX}`
  if (band.min <= SCORE_MIN) return `< ${band.max.toFixed(1)}`
  return `${band.min.toFixed(1)}–${(band.max - 0.1).toFixed(1)}`
}

/**
 * Text color that reads against scoreToColor(score). White on the dark fills,
 * near-black on amber — the map markers print the number inside the circle, so
 * this has to be derived from the same table rather than guessed per component.
 */
export function scoreToInk(score) {
  return scoreToBand(score).ink
}

export function clampScore(score) {
  return Math.min(SCORE_MAX, Math.max(SCORE_MIN, score))
}

/** Score → 0–180° sweep position along the speedometer arc (0 = far left). */
export function scoreToAngle(score) {
  const s = clampScore(Number(score) || 0)
  return (s / SCORE_MAX) * 180
}

/** "8.4" — one decimal, em-dash when the index is missing. */
export function formatScore(score) {
  if (score === null || score === undefined || Number.isNaN(Number(score))) return '—'
  return Number(score).toFixed(1)
}

/* ─────────────────────────────────────────────────────────────────────────
   THE UV INDEX IS A DIFFERENT SCALE. DO NOT REUSE ANYTHING ABOVE FOR IT.

   Everything above is the beach quality score: 0–10, HIGHER IS BETTER.
   The UV index below is the EPA scale: 0 to 11+, HIGHER IS WORSE.

   They are not rescalings of each other and they point opposite ways. The API
   sends both — `uv_index` (the measurement) and `subscores.uv` (a 0–10 quality
   term derived from it). You cannot recover one from the other: scoring.py's
   uv_score() is clip(10 - max(0, uv-3)*1.1, 0, 10), which clamps, so every
   index at or below 3 collapses to a sub-score of 10.

   An earlier version of BeachCard inverted the sub-score to guess the index.
   That is the bug this block exists to prevent. Always read `uv_index`.
   ───────────────────────────────────────────────────────────────────────── */

/** EPA UV index category. Input is the raw index, NOT the sub-score. */
export function uvIndexToLabel(uvIndex) {
  if (uvIndex === null || uvIndex === undefined || Number.isNaN(Number(uvIndex))) {
    return null
  }
  const uv = Number(uvIndex)
  if (uv <= 2) return 'Low'
  if (uv <= 5) return 'Moderate'
  if (uv <= 7) return 'High'
  if (uv <= 10) return 'Very High'
  return 'Extreme'
}

/** "7 (High)" — the number and its category, since neither alone is obvious. */
export function formatUvIndex(uvIndex) {
  const label = uvIndexToLabel(uvIndex)
  if (label === null) return null
  return `${Math.round(Number(uvIndex))} (${label})`
}
