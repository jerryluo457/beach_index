import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import BeachCard from '../components/BeachCard.jsx'
import BeachMap from '../components/BeachMap.jsx'
import { fetchBeaches, ingestAge, MOCK, POLLABLE, pollBeach } from '../api.js'

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
  const [refreshing, setRefreshing] = useState(false)
  const [refreshError, setRefreshError] = useState(null)

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

  /**
   * Scrape both cameras now, then re-read /beaches.
   *
   * allSettled, not all: one dead camera must not throw away the frame the
   * other one just fetched. The reload runs regardless, for the same reason —
   * if either poll landed, the page should show it.
   */
  const refresh = useCallback(async () => {
    setRefreshing(true)
    setRefreshError(null)
    try {
      const results = await Promise.allSettled(POLLABLE.map((id) => pollBeach(id)))
      const failed = results
        .map((r, i) => [POLLABLE[i], r])
        .filter(([, r]) => r.status === 'rejected' || r.value?.status === 'failed')
      if (failed.length === POLLABLE.length) {
        const [, first] = failed[0]
        setRefreshError(first.reason?.message ?? 'poll failed')
      }
      setBeaches(orderBeaches(await fetchBeaches()))
    } catch (err) {
      // The existing cards stay on screen: a failed refresh must degrade to
      // stale data, never to a blank dashboard.
      setRefreshError(err.message)
    } finally {
      setRefreshing(false)
    }
  }, [])

  const lastUpdated = beaches
    .map((b) => b.updated_at)
    .filter(Boolean)
    .sort()
    .at(-1)

  const age = ingestAge(lastUpdated)

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
            {/* The date is only omitted when the ingest IS from today — see
                ingestAge() for why a bare time here was actively misleading. */}
            <dd className={age?.isStale ? 'is-stale' : undefined}>
              {age ? age.label : '—'}
              {age?.isStale && ' · stale'}
              {!MOCK && (
                <button
                  type="button"
                  className="linklike"
                  onClick={refresh}
                  disabled={refreshing}
                >
                  {refreshing ? 'Refreshing…' : 'Refresh'}
                </button>
              )}
              {refreshError && (
                <span className="refresh-error" role="status">
                  {refreshError}
                </span>
              )}
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
