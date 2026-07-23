/**
 * api.test.js — tests for ingestAge(), the staleness helper.
 *
 * Run:  node src/api.test.js
 *
 * Same plain-Node-and-assert approach as score.test.js: no framework, no extra
 * dependencies.
 *
 * WHY THIS FUNCTION IS WORTH TESTING. It exists because of a real misreading.
 * The masthead printed `updated_at` as time-only next to a "Reading" line
 * showing today's date, so a frame ingested at 7:05 PM the previous evening
 * rendered as "Thursday, July 23 · 07:05 PM" — indistinguishable from a fresh
 * reading, and at 9 AM apparently ten hours in the future. The launchd poller
 * had been dead since the previous evening and nothing on the page said so.
 *
 * Both halves of the contract are therefore load-bearing:
 *   - anything not from today MUST carry its date
 *   - anything older than two poll cycles MUST be flagged stale
 * `now` is injectable precisely so these can be asserted without waiting.
 */

import assert from 'node:assert/strict'
import { ingestAge, STALE_AFTER_MS } from './api.js'

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

// A fixed "now" so these assertions don't drift with the wall clock.
const NOW = new Date('2026-07-23T09:10:00-04:00').getTime()
const ago = (ms) => new Date(NOW - ms).toISOString()

const MINUTE = 60 * 1000
const HOUR = 60 * MINUTE

console.log('Freshness')

check('a reading from minutes ago is neither stale nor dated', () => {
  const age = ingestAge(ago(20 * MINUTE), NOW)
  assert.equal(age.isToday, true)
  assert.equal(age.isStale, false)
  // Time only — the date would be noise when it is obviously today.
  assert.match(age.label, /^\d{2}:\d{2} (AM|PM)$/)
})

check('one missed poll is not yet stale', () => {
  // Deliberately forgiving: a single transient scrape failure is normal and
  // must not cry wolf.
  assert.equal(ingestAge(ago(90 * MINUTE), NOW).isStale, false)
})

check('two missed polls is stale', () => {
  assert.equal(ingestAge(ago(STALE_AFTER_MS + MINUTE), NOW).isStale, true)
})

console.log('\nThe bug this was written for')

check('last nights 7:05 PM ingest is dated AND stale', () => {
  // The exact reading that was on screen when the poller was found dead.
  const age = ingestAge('2026-07-22T23:05:16.382613+00:00', NOW)
  assert.equal(age.isToday, false)
  assert.equal(age.isStale, true)
  // The date is the whole point — without it this read as "07:05 PM" today.
  assert.match(age.label, /Jul 22/)
  assert.match(age.label, /07:05 PM/)
})

check('yesterday carries its date even when only minutes old', () => {
  // 00:05 local, ingested 23:55 the previous day: ten minutes ago, so NOT
  // stale, but still a different calendar day and so still dated. Staleness
  // and date-ness are independent, and conflating them would drop the date
  // exactly when midnight makes it most confusing.
  const justAfterMidnight = new Date('2026-07-23T00:05:00-04:00').getTime()
  const age = ingestAge('2026-07-22T23:55:00-04:00', justAfterMidnight)
  assert.equal(age.isStale, false)
  assert.equal(age.isToday, false)
  assert.match(age.label, /Jul 22/)
})

console.log('\nAbsent and malformed input')

check('no timestamp is null, not a stale reading', () => {
  // "never ingested" and "ingested long ago" are different facts: the first
  // renders an em dash, the second an amber warning. Returning a stale-looking
  // object for null would put a warning on a beach that has no camera at all.
  assert.equal(ingestAge(null, NOW), null)
  assert.equal(ingestAge(undefined, NOW), null)
  assert.equal(ingestAge('', NOW), null)
})

check('an unparseable timestamp is null, not Invalid Date', () => {
  assert.equal(ingestAge('not a date', NOW), null)
})

if (failures > 0) {
  console.log(`\n${failures} FAILED`)
  process.exit(1)
}
console.log('\nall api.js tests passed')
