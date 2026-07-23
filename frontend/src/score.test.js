/**
 * score.test.js — boundary tests for the five-band mapping.
 *
 * Run:  node src/score.test.js
 *
 * No test framework: this is plain Node with assert, so it runs with zero
 * extra dependencies. Boundaries are the whole point — the bands are
 * half-open [min, max), and an off-by-one here silently miscolors every gauge,
 * map marker and detail bar in the app at once.
 */

import assert from 'node:assert/strict'
import {
  SCORE_BANDS,
  UNKNOWN_BAND,
  bandRangeLabel,
  formatScore,
  formatUvIndex,
  scoreToAngle,
  scoreToBand,
  scoreToColor,
  scoreToInk,
  scoreToLabel,
  uvIndexToLabel,
} from './score.js'

let failures = 0
function check(name, fn) {
  try {
    fn()
    console.log(`  PASS  ${name}`)
  } catch (err) {
    console.log(`  FAIL  ${name}\n          ${err.message}`)
    failures += 1
  }
}

console.log('Band boundaries (half-open, lower-inclusive)')

// The exact boundaries the spec calls out: Good is [6.0, 8.0), Fair [5.0, 6.0).
const cases = [
  [0, 'Poor'],
  [4.9, 'Poor'],
  [4.99, 'Poor'],
  [5.0, 'Fair'], // lower bound is INCLUSIVE
  [5.5, 'Fair'],
  [5.9, 'Fair'],
  [6.0, 'Good'], // Fair's upper bound is EXCLUSIVE
  [7.0, 'Good'],
  [7.9, 'Good'],
  [8.0, 'Very Good'], // Good's upper bound is EXCLUSIVE
  [8.9, 'Very Good'],
  [9.0, 'Excellent'],
  [10, 'Excellent'], // top end is CLOSED
]
for (const [score, label] of cases) {
  check(`${score} -> ${label}`, () => assert.equal(scoreToLabel(score), label))
}

console.log('\nOut-of-range and missing values')
check('above 10 clamps into Excellent', () => assert.equal(scoreToLabel(11), 'Excellent'))
check('negative clamps into Poor', () => assert.equal(scoreToLabel(-3), 'Poor'))
check('null -> No data', () => assert.equal(scoreToBand(null), UNKNOWN_BAND))
check('undefined -> No data', () => assert.equal(scoreToBand(undefined), UNKNOWN_BAND))
check('NaN -> No data', () => assert.equal(scoreToBand(NaN), UNKNOWN_BAND))
check('numeric string still works', () => assert.equal(scoreToLabel('6.5'), 'Good'))

console.log('\nBand table integrity')
check('bands are contiguous and gapless', () => {
  for (let i = 1; i < SCORE_BANDS.length; i += 1) {
    assert.equal(
      SCORE_BANDS[i].min,
      SCORE_BANDS[i - 1].max,
      `gap between ${SCORE_BANDS[i - 1].label} and ${SCORE_BANDS[i].label}`,
    )
  }
})
check('bands span the full 0-10 range', () => {
  assert.equal(SCORE_BANDS[0].min, 0)
  assert.equal(SCORE_BANDS[SCORE_BANDS.length - 1].max, 10)
})
check('every band has a distinct color', () => {
  const colors = new Set(SCORE_BANDS.map((b) => b.color))
  assert.equal(colors.size, SCORE_BANDS.length)
})
check('every score in 0..10 maps to some band', () => {
  for (let s = 0; s <= 100; s += 1) {
    const band = scoreToBand(s / 10)
    assert.ok(band && band !== UNKNOWN_BAND, `no band for ${s / 10}`)
  }
})

console.log('\nColor / ink pairing')
check('color and ink come from the same band', () => {
  assert.equal(scoreToColor(6.5), SCORE_BANDS[2].color)
  assert.equal(scoreToInk(6.5), SCORE_BANDS[2].ink)
})
check('amber band uses dark ink for contrast', () => {
  // Good is the light fill; white text on it would be unreadable on the map.
  assert.equal(scoreToInk(7), '#231B02')
})

console.log('\nRange labels (must not imply a closed upper bound)')
check('Poor', () => assert.equal(bandRangeLabel(SCORE_BANDS[0]), '< 5.0'))
check('Fair', () => assert.equal(bandRangeLabel(SCORE_BANDS[1]), '5.0–5.9'))
check('Good spans two points', () => assert.equal(bandRangeLabel(SCORE_BANDS[2]), '6.0–7.9'))
check('Very Good', () => assert.equal(bandRangeLabel(SCORE_BANDS[3]), '8.0–8.9'))
check('Excellent closes at 10', () => assert.equal(bandRangeLabel(SCORE_BANDS[4]), '9.0–10'))

console.log('\nGauge geometry')
check('0 -> 0deg (far left)', () => assert.equal(scoreToAngle(0), 0))
check('10 -> 180deg (far right)', () => assert.equal(scoreToAngle(10), 180))
check('5 -> 90deg (top)', () => assert.equal(scoreToAngle(5), 90))
check('6.0 sits where the Good band starts', () => {
  // The needle must land inside the band its label claims.
  const angle = scoreToAngle(6)
  const bandStart = (SCORE_BANDS[2].min / 10) * 180
  assert.equal(angle, bandStart)
})

console.log('\nformatScore')
check('one decimal', () => assert.equal(formatScore(8.44), '8.4'))
check('integer gets a decimal', () => assert.equal(formatScore(9), '9.0'))
check('null -> em dash', () => assert.equal(formatScore(null), '—'))

console.log('\nUV index is a DIFFERENT scale (higher = worse)')
check('0 -> Low', () => assert.equal(uvIndexToLabel(0), 'Low'))
check('2 -> Low', () => assert.equal(uvIndexToLabel(2), 'Low'))
check('3 -> Moderate', () => assert.equal(uvIndexToLabel(3), 'Moderate'))
check('5 -> Moderate', () => assert.equal(uvIndexToLabel(5), 'Moderate'))
check('6 -> High', () => assert.equal(uvIndexToLabel(6), 'High'))
check('8 -> Very High', () => assert.equal(uvIndexToLabel(8), 'Very High'))
check('11 -> Extreme', () => assert.equal(uvIndexToLabel(11), 'Extreme'))
check('12 -> Extreme', () => assert.equal(uvIndexToLabel(12), 'Extreme'))
check('null -> null', () => assert.equal(uvIndexToLabel(null), null))
check('formats number and category', () => assert.equal(formatUvIndex(2), '2 (Low)'))
check('the 6pm real-world case', () => assert.equal(formatUvIndex(2), '2 (Low)'))

// A UV index of 0 after sunset is a READING, not a missing value. The backend
// had a matching bug where `0 or fallback` discarded it; make sure the display
// layer can't reintroduce the same confusion.
check('0 is a real reading, not "no data"', () => {
  assert.equal(uvIndexToLabel(0), 'Low')
  assert.equal(formatUvIndex(0), '0 (Low)')
})
check('0 is distinguishable from null', () => {
  assert.notEqual(formatUvIndex(0), formatUvIndex(null))
  assert.equal(formatUvIndex(null), null)
})
check('UV scale is not the beach scale', () => {
  // A UV index of 2 is GOOD news; a beach score of 2 is terrible. If these
  // ever share a helper, this assertion is the tripwire.
  assert.equal(uvIndexToLabel(2), 'Low')
  assert.equal(scoreToLabel(2), 'Poor')
})

if (failures > 0) {
  console.log(`\n${failures} FAILED`)
  process.exit(1)
}
console.log('\nall score.js tests passed')
