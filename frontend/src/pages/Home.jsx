import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import BeachCard from '../components/BeachCard.jsx'
import BeachMap from '../components/BeachMap.jsx'
import { fetchBeaches, MOCK } from '../api.js'

/**
 * Home — the dashboard: four cards in a row, full-width map beneath.
 *
 * No recommendation banner and no ranking. Cards render in fixed geographic
 * order, north to south, so a beach doesn't move position between refreshes;
 * each card stands on its own.
 *
 * Home owns the hover state shared by the card row and the map, which is why
 * it lives here rather than inside either component.
 */

const GEOGRAPHIC_ORDER = ['jupiter', 'lake-worth', 'boynton', 'boca']

function orderBeaches(beaches) {
  return [...beaches].sort(
    (a, b) => GEOGRAPHIC_ORDER.indexOf(a.id) - GEOGRAPHIC_ORDER.indexOf(b.id),
  )
}

const STAMP = new Intl.DateTimeFormat('en-US', {
  weekday: 'long',
  month: 'long',
  day: 'numeric',
})

export default function Home() {
  const [beaches, setBeaches] = useState([])
  const [status, setStatus] = useState('loading')
  const [error, setError] = useState(null)
  const [activeId, setActiveId] = useState(null)

  useEffect(() => {
    let cancelled = false
    fetchBeaches()
      .then((data) => {
        if (cancelled) return
        setBeaches(orderBeaches(data))
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
  }, [])

  const lastUpdated = beaches
    .map((b) => b.updated_at)
    .filter(Boolean)
    .sort()
    .at(-1)

  return (
    <div className="shell">
      <header className="masthead">
        <div>
          <p className="masthead__eyebrow">Palm Beach County · Atlantic Coast</p>
          <h1 className="masthead__title">
            Beach <em>Index</em>
          </h1>
        </div>
        <div className="masthead__meta">
          <dl>
            <dt>Reading</dt>
            <dd>{STAMP.format(new Date())}</dd>
          </dl>
          <dl>
            <dt>Last ingest</dt>
            <dd>
              {lastUpdated
                ? new Date(lastUpdated).toLocaleTimeString('en-US', {
                    hour: '2-digit',
                    minute: '2-digit',
                  })
                : '—'}
            </dd>
          </dl>
          <dl>
            <dt>Source</dt>
            <dd>{MOCK ? 'Mock fixtures' : 'Live'}</dd>
          </dl>
        </div>
      </header>

      {status === 'loading' && (
        <div className="skeleton-row">
          {[0, 1, 2, 3].map((i) => (
            <div className="skeleton" key={i} />
          ))}
        </div>
      )}

      {status === 'error' && (
        <p className="state state--error">Couldn’t load conditions — {error}</p>
      )}

      {status === 'ready' && (
        <>
          <div className="card-row">
            {beaches.map((beach, i) => (
              <BeachCard key={beach.id} beach={beach} index={i} onHover={setActiveId} />
            ))}
          </div>

          <div className="section-head">
            <span className="section-head__label">Station map</span>
            <span className="section-head__rule" />
            <span className="section-head__note">{beaches.length} stations · click a marker</span>
          </div>

          <BeachMap beaches={beaches} activeId={activeId} onHover={setActiveId} />
        </>
      )}

      <footer className="footer">
        <span>Sargassum U-Net · SAHI crowd count · NWS &amp; NOAA feeds</span>
        <Link to="/upload">Ingest console</Link>
      </footer>
    </div>
  )
}
