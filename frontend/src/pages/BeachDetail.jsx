import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import Speedometer from '../components/Speedometer.jsx'
import CamFeed from '../components/CamFeed.jsx'
import ForecastTimeline from '../components/ForecastTimeline.jsx'
import { fetchBeach, FEATURES, ingestAge } from '../api.js'
import {
  SCORE_BANDS,
  formatScore,
  formatUvIndex,
  scoreToBand,
  scoreToColor,
} from '../score.js'

/**
 * BeachDetail — /beach/:id. The full breakdown behind a card: one large
 * speedometer for the overall index, then every sub-score on its own banded
 * track using the same five colors, then labeled placeholders for the three
 * sections that aren't built yet.
 *
 * This page carries every raw measurement. The home cards deliberately stay
 * minimal — anything more detailed than their six stats belongs here, behind
 * the click.
 *
 * NOTE ON THE TWO UV NUMBERS. This page shows both "UV Index — 12 (Extreme)"
 * in the fact grid and "UV Exposure — 0.1" in the sub-scores. They are not
 * inconsistent: the first is the EPA measurement (higher = worse), the second
 * is its contribution to the beach quality index (higher = better). The labels
 * carry that distinction, so keep them distinct.
 */

/** Plain-language consequence of each NWS rip current category. */
const RIP_DESCRIPTIONS = {
  low: 'Low risk — conditions are typical. Still swim near a lifeguard.',
  moderate: 'Moderate risk — rip currents are likely near piers, jetties and inlets.',
  high: 'High risk — dangerous rip currents. Entering the water is not advised.',
}

const titleCase = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s)

const SUBSCORES = [
  {
    key: 'weather',
    name: 'Weather',
    blurb: 'Air temp, humidity, wind and precipitation probability from the NWS forecast.',
  },
  {
    key: 'sargassum',
    name: 'Sargassum',
    blurb: 'Sand coverage measured by the U-Net segmenter on this morning’s cam frame.',
  },
  { key: 'rip', name: 'Rip Current', blurb: 'NWS surf-zone rip current risk for this stretch of coast.' },
  { key: 'water', name: 'Water Quality', blurb: 'Water severity classifier — beta model, not yet validated.' },
  { key: 'crowd', name: 'Crowd', blurb: 'People counted in the frame by YOLO with SAHI slicing — beta.' },
  {
    key: 'uv',
    name: 'UV Exposure',
    // Spelled out because this card sits near "UV Index" in the fact grid and
    // the two numbers run in opposite directions.
    blurb: 'Derived from the EPA UV index and inverted — a high score means low exposure.',
  },
]

/** A sub-score printed as the five bands with a pin at the exact value. */
function SubScoreBar({ value }) {
  const has = value !== null && value !== undefined
  const active = scoreToBand(value).label
  return (
    <div className="subscore__track">
      {SCORE_BANDS.map((band) => (
        <span
          key={band.label}
          className={`subscore__band${has && band.label === active ? ' is-active' : ''}`}
          style={{
            background: band.color,
            flexGrow: band.max - band.min,
          }}
        />
      ))}
      {has && <span className="subscore__pin" style={{ left: `${(value / 10) * 100}%` }} />}
    </div>
  )
}

// The three sub-scores that come from a cam frame rather than a public feed.
// For these, "missing" has two very different causes worth telling apart.
const CAMERA_SUBSCORES = ['sargassum', 'water', 'crowd']

/**
 * What to say when a sub-score has no value.
 *
 * The old copy said "No signal for this beach" for every gap, which is only
 * true for the beaches that have no camera. On a beach that DOES have one, the
 * same sentence quietly misattributed a broken model to a missing feature —
 * which is exactly what a dead crowd counter looked like for hours: an em dash
 * and a line implying the app was never going to show that number here.
 */
function missingBlurb(key, beach) {
  if (CAMERA_SUBSCORES.includes(key) && !beach.supported) {
    return 'No analysed camera for this beach — excluded from the index.'
  }
  if (CAMERA_SUBSCORES.includes(key)) {
    return 'Not measured in the latest frame — the model did not return a value. Excluded from the index.'
  }
  return 'Feed unavailable right now — excluded from the index.'
}

function Fact({ label, value }) {
  const absent = value === null || value === undefined || value === ''
  return (
    <div className="fact">
      <div className="fact__label">{label}</div>
      <div className={`fact__value${absent ? ' is-absent' : ''}`}>{absent ? '—' : value}</div>
    </div>
  )
}

function Placeholder({ label, children }) {
  return (
    <div className="placeholder">
      <div className="placeholder__label">
        {label}
        <span className="placeholder__flag">Feature flag off</span>
      </div>
      <p className="placeholder__copy">{children}</p>
    </div>
  )
}

export default function BeachDetail() {
  const { id } = useParams()
  const [beach, setBeach] = useState(null)
  const [status, setStatus] = useState('loading')
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    fetchBeach(id)
      .then((data) => {
        if (cancelled) return
        setBeach(data)
        setStatus('ready')
      })
      .catch((err) => {
        if (cancelled) return
        setError(err.message)
        setStatus('error')
      })
    return () => {
      cancelled = true
    }
  }, [id])

  // Handed to CamFeed so a manual re-poll refreshes this page from the server
  // rather than from a patched-up local copy of `beach`. Deliberately does NOT
  // set status back to 'loading': the panel should update in place, not tear
  // the whole page down to a spinner.
  const reload = useCallback(async () => {
    setBeach(await fetchBeach(id))
  }, [id])

  const age = ingestAge(beach?.updated_at)

  if (status === 'loading') return <div className="shell"><p className="state">Loading station…</p></div>
  if (status === 'error') {
    return (
      <div className="shell">
        <Link to="/" className="back-link">← All beaches</Link>
        <p className="state state--error">{error}</p>
      </div>
    )
  }

  const band = scoreToBand(beach.index)

  return (
    <div className="shell">
      <Link to="/" className="back-link">← All beaches</Link>

      <section className="detail-hero">
        <Speedometer score={beach.index} size={290} valueSize={46} labelSize={13} />

        <div>
          <h1 className="detail-hero__name">{beach.name}</h1>
          <p className="detail-hero__forecast">
            {beach.short_forecast ?? 'Forecast unavailable'} · Overall{' '}
            <span style={{ color: band.color }}>{band.label}</span>
          </p>

          <div className="fact-grid">
            <Fact label="Air temp" value={beach.temp_f != null ? `${Math.round(beach.temp_f)}°F` : null} />
            <Fact
              label="Water temp"
              value={beach.water_temp_f != null ? `${Math.round(beach.water_temp_f)}°F` : null}
            />
            <Fact
              label="Next tide"
              value={
                beach.next_tide
                  ? `${beach.next_tide}${
                      beach.next_tide_height_ft != null
                        ? ` · ${beach.next_tide_height_ft.toFixed(1)} ft`
                        : ''
                    }`
                  : null
              }
            />
            {/* "UV Index" — the EPA measurement, higher = worse. Distinct from
                the "UV Exposure" sub-score below, which runs the other way. */}
            <Fact label="UV index" value={formatUvIndex(beach.uv_index)} />
            <Fact label="Wind" value={beach.wind_mph != null ? `${Math.round(beach.wind_mph)} mph` : null} />
            <Fact
              label="Humidity"
              value={beach.humidity_pct != null ? `${Math.round(beach.humidity_pct)}%` : null}
            />
            <Fact
              label="Rain chance"
              value={beach.precip_prob != null ? `${Math.round(beach.precip_prob)}%` : null}
            />
            <Fact
              label="Sargassum"
              value={
                !beach.supported
                  ? 'Camera not supported'
                  : beach.coverage_pct != null
                    ? `${beach.coverage_pct.toFixed(2)}% · ${beach.sargassum_label ?? '—'}`
                    : beach.sargassum_label
              }
            />
            <Fact
              label="Crowd"
              value={
                // != null, not truthiness: a count of 0 is a real measurement
                // (an empty beach), and `0 ? … : …` would report it as "no
                // reading" — the same trap the UV index already documents.
                beach.crowd_count != null
                  ? `${beach.crowd_count} ${beach.crowd_count === 1 ? 'person' : 'people'} · ${beach.crowd_label ?? '—'}`
                  : beach.crowd_label
              }
            />
            {/* Same wording as the masthead and the cam panel — three places
                showing the same timestamp in three formats was how a stale
                reading went unnoticed in the first place. */}
            <Fact
              label="Last ingest"
              value={age ? `${age.label}${age.isStale ? ' · stale' : ''}` : 'Never'}
            />
          </div>

          {/* The one genuine safety signal in the payload, so it gets its own
              line rather than a cell in the grid. */}
          {beach.rip_risk && (
            <p
              className="advisory"
              data-risk={beach.rip_risk}
              role={beach.rip_risk === 'high' ? 'alert' : undefined}
            >
              <span className="advisory__label">
                Rip current · {titleCase(beach.rip_risk)}
              </span>
              {RIP_DESCRIPTIONS[beach.rip_risk] ?? ''}
            </p>
          )}
        </div>
      </section>

      <div className="section-head">
        <span className="section-head__label">Sub-scores</span>
        <span className="section-head__rule" />
        <span className="section-head__note">geometric mean · missing signals renormalized</span>
      </div>

      <div className="subscores">
        {SUBSCORES.map((sub, i) => {
          const value = beach.subscores?.[sub.key]
          const has = value !== null && value !== undefined
          return (
            <article className="subscore" key={sub.key} style={{ animationDelay: `${i * 60}ms` }}>
              <div className="subscore__head">
                <span className="subscore__name">{sub.name}</span>
                <span
                  className="subscore__value"
                  style={{ color: has ? scoreToColor(value) : 'var(--ink-faint)' }}
                >
                  {formatScore(value)}
                </span>
              </div>
              <SubScoreBar value={value} />
              <p className="subscore__blurb">
                {has ? sub.blurb : missingBlurb(sub.key, beach)}
              </p>
            </article>
          )
        })}
      </div>

      {FEATURES.llmSummary && beach.plain_summary && (
        <>
          <div className="section-head">
            <span className="section-head__label">In plain language</span>
            <span className="section-head__rule" />
            <span className="section-head__note">generated locally</span>
          </div>
          <div className="summary-callout">
            <p className="summary-callout__text">{beach.plain_summary}</p>
            <p className="summary-callout__disclaimer">
              Written from the readings above by a small language model running on this
              machine, with its output checked against those readings before display.
              Supplementary only — the rip current advisory and posted signs at the beach
              are authoritative.
            </p>
          </div>
        </>
      )}

      {FEATURES.camFeed && (
        <>
          <div className="section-head">
            <span className="section-head__label">Cam feed</span>
            <span className="section-head__rule" />
            <span className="section-head__note">
              {beach.supported ? 'model overlays' : 'external source'}
            </span>
          </div>
          <CamFeed beach={beach} onRefreshed={reload} />
        </>
      )}

      {FEATURES.forecastTimeline && (
        <>
          <div className="section-head">
            <span className="section-head__label">Index history</span>
            <span className="section-head__rule" />
            <span className="section-head__note">one point per ingest</span>
          </div>
          <ForecastTimeline beachId={id} />
        </>
      )}

      <footer className="footer">
        <span>Station {beach.id} · {beach.lat.toFixed(4)}, {beach.lon.toFixed(4)}</span>
        <Link to="/upload">Ingest console</Link>
      </footer>
    </div>
  )
}
