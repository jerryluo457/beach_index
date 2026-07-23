/**
 * api.js — the ONLY module that talks to the backend.
 *
 * Every network call in the app goes through here. Components import functions,
 * never URLs. That keeps the MOCK switch below a single flip: with MOCK = true
 * the whole dashboard runs with no Python process at all, and the fabricated
 * payloads deliberately mirror main.py's BeachSummary / IngestResult shapes so
 * that flipping to false is a non-event.
 */

// Live. Flip to true to run the whole dashboard off the fixtures below with no
// Python process at all — useful when the backend is down or you're working on
// a plane. The fixtures mirror BeachSummary field-for-field, so the flip stays
// a non-event in both directions.
export const MOCK = false

// Vite proxies /api → http://127.0.0.1:8000 in dev (see vite.config.js).
const BASE = import.meta.env.VITE_API_BASE ?? '/api'

/**
 * Backend-relative media path ("/media/foo.jpg") → a URL this page can load.
 *
 * The backend serves cam frames and overlays from its own /media mount, so the
 * path needs the same BASE prefix every fetch() gets — otherwise in dev the
 * browser asks Vite (:5173) for /media/... and gets the SPA's index.html back
 * instead of an image.
 */
export function mediaUrl(path) {
  if (!path) return null
  if (/^https?:\/\//.test(path)) return path
  return `${BASE}${path}`
}

/** Sargassum model is only validated on these two cameras. */
export const SARGASSUM_SUPPORTED = ['lake-worth', 'boynton']

/** Beach ids + display names, mirroring sources.py's BEACHES dict. */
export const BEACH_OPTIONS = [
  { id: 'jupiter', name: 'Jupiter Inlet' },
  { id: 'lake-worth', name: 'Lake Worth Inlet' },
  { id: 'boynton', name: 'Boynton Inlet' },
  { id: 'boca', name: 'Boca Raton — Spanish River Park' },
]

/**
 * Feature flags for the detail page's not-yet-built sections. They render as
 * labeled placeholders while false so the layout is real and reviewable.
 */
export const FEATURES = {
  camFeed: true,
  forecastTimeline: true,
  llmSummary: true,
}

// ─────────────────────────────────────────────────────────────────────────
// MOCK FIXTURES — shapes mirror backend/main.py exactly
// ─────────────────────────────────────────────────────────────────────────

const MOCK_BEACHES = [
  {
    id: 'jupiter',
    name: 'Jupiter Inlet',
    lat: 26.941915,
    lon: -80.071898,
    index: 7.2,
    subscores: { weather: 7.6, sargassum: null, rip: 6.4, water: null, crowd: null, uv: 5.1 },
    temp_f: 78,
    water_temp_f: 81,
    sargassum_label: null,
    crowd_label: null,
    supported: false,
    short_forecast: 'Partly Sunny',
    updated_at: '2026-07-22T07:14:00Z',
    next_tide: 'Low at 13:42',
    next_tide_height_ft: 0.4,
    uv_index: 9,
    rip_risk: 'moderate',
    coverage_pct: null,
    crowd_count: null,
    humidity_pct: 70,
    precip_prob: 12,
    wind_mph: 9,
  },
  {
    id: 'lake-worth',
    name: 'Lake Worth Inlet',
    lat: 26.767776,
    lon: -80.035963,
    index: 8.4,
    subscores: { weather: 8.1, sargassum: 8.8, rip: 7.9, water: 8.2, crowd: 9.0, uv: 5.4 },
    temp_f: 78,
    water_temp_f: 81,
    sargassum_label: 'Mild',
    crowd_label: 'Quiet',
    supported: true,
    short_forecast: 'Mostly Sunny',
    updated_at: '2026-07-22T07:02:00Z',
    next_tide: 'Low at 13:31',
    next_tide_height_ft: 0.3,
    uv_index: 7,
    rip_risk: 'low',
    coverage_pct: 0.62,
    crowd_count: 13,
    humidity_pct: 72,
    precip_prob: 4,
    wind_mph: 13,
  },
  {
    id: 'boynton',
    name: 'Boynton Inlet',
    lat: 26.543465,
    lon: -80.043413,
    index: 9.1,
    subscores: { weather: 8.9, sargassum: 9.4, rip: 9.2, water: 8.8, crowd: 9.6, uv: 5.4 },
    temp_f: 78,
    water_temp_f: 81,
    sargassum_label: 'Mild',
    crowd_label: 'Quiet',
    supported: true,
    short_forecast: 'Sunny',
    updated_at: '2026-07-22T07:06:00Z',
    next_tide: 'Low at 13:31',
    next_tide_height_ft: 0.3,
    uv_index: 7,
    rip_risk: 'low',
    coverage_pct: 0.41,
    crowd_count: 6,
    humidity_pct: 68,
    precip_prob: 2,
    wind_mph: 11,
  },
  {
    id: 'boca',
    name: 'Boca Raton — Spanish River Park',
    lat: 26.379405,
    lon: -80.06717,
    index: 6.3,
    subscores: { weather: 6.8, sargassum: null, rip: 5.2, water: null, crowd: null, uv: 4.6 },
    temp_f: 78,
    water_temp_f: 81,
    sargassum_label: null,
    crowd_label: null,
    supported: false,
    short_forecast: 'Scattered Showers',
    updated_at: '2026-07-22T06:58:00Z',
    next_tide: 'Low at 13:22',
    next_tide_height_ft: 0.5,
    uv_index: 10,
    rip_risk: 'high',
    coverage_pct: null,
    crowd_count: null,
    humidity_pct: 78,
    precip_prob: 55,
    wind_mph: 16,
  },
]

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function request(path, options) {
  const res = await fetch(`${BASE}${path}`, options)
  if (!res.ok) {
    // FastAPI puts the human-readable reason in `detail`; surface that rather
    // than a bare status code, because the upload page prints it verbatim.
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch {
      /* non-JSON error body — keep the status line */
    }
    throw new Error(detail)
  }
  return res.json()
}

// ─────────────────────────────────────────────────────────────────────────
// PUBLIC API
// ─────────────────────────────────────────────────────────────────────────

/** GET /beaches → BeachSummary[] */
export async function fetchBeaches() {
  if (MOCK) {
    await delay(320)
    return structuredClone(MOCK_BEACHES)
  }
  return request('/beaches')
}

/** GET /beach/{id} → BeachSummary */
export async function fetchBeach(beachId) {
  if (MOCK) {
    await delay(260)
    const beach = MOCK_BEACHES.find((b) => b.id === beachId)
    if (!beach) throw new Error(`Unknown beach '${beachId}'`)
    return structuredClone(beach)
  }
  return request(`/beach/${beachId}`)
}

/** GET /history/{id} → { beach_id, readings[] } */
export async function fetchHistory(beachId, limit = 30) {
  if (MOCK) {
    await delay(200)
    return { beach_id: beachId, readings: [] }
  }
  return request(`/history/${beachId}?limit=${limit}`)
}

/**
 * POST /ingest/{beachId} as multipart/form-data → IngestResult.
 *
 * The field name MUST stay "file" — it matches FastAPI's `file: UploadFile`
 * parameter, and a mismatch fails as a 422 that looks like a validation bug.
 *
 * This runs the sargassum U-Net and the SAHI-sliced crowd counter
 * synchronously, so it is slow by design (10–15s is normal). Callers must show
 * a loading state; do not add a timeout short enough to kill a real inference.
 */
export async function ingestBeach(beachId, file) {
  if (MOCK) {
    await delay(2600)
    const supported = SARGASSUM_SUPPORTED.includes(beachId)
    return {
      beach: beachId,
      coverage_pct: supported ? 4.8 : null,
      sargassum_label: supported ? 'Mild' : null,
      crowd_count: 23,
      water_severity: 'clear',
      stored_at: new Date().toISOString(),
    }
  }

  const form = new FormData()
  form.append('file', file)
  return request(`/ingest/${beachId}`, { method: 'POST', body: form })
}
