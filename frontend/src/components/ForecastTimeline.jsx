import { useEffect, useMemo, useState } from 'react'
import { fetchHistory } from '../api.js'
import { SCORE_BANDS, formatScore, scoreToColor, scoreToLabel } from '../score.js'

/**
 * ForecastTimeline — the stored index over time, as an SVG line chart.
 *
 * Hand-rolled rather than pulling in a charting library: the project has no
 * charting dependency, the Speedometer already establishes the pattern of
 * drawing SVG directly, and a library would bring its own color system to
 * fight with score.js.
 *
 * OVERNIGHT GAPS ARE REAL DATA, NOT MISSING DATA. The cameras publish nothing
 * after dark and the poller enforces a daylight window, so a multi-day series
 * has dense daytime clusters separated by long empty nights. Connecting across
 * those would draw a confident line through hours nobody measured, so the path
 * is deliberately BROKEN wherever consecutive points are more than GAP_HOURS
 * apart.
 *
 * Points are colored with scoreToColor() — the same function driving the
 * gauges and map markers, so a dot's color always agrees with the card.
 */

const W = 900
const H = 220
const PAD = { top: 14, right: 16, bottom: 26, left: 30 }
const GAP_HOURS = 3

export default function ForecastTimeline({ beachId }) {
  const [readings, setReadings] = useState(null)
  const [error, setError] = useState(null)
  const [hover, setHover] = useState(null)

  useEffect(() => {
    let cancelled = false
    setReadings(null)
    setError(null)
    fetchHistory(beachId, 60)
      .then((data) => {
        if (!cancelled) setReadings(data.readings ?? [])
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [beachId])

  // Only readings that actually carry an index can be plotted. Rows ingested
  // before index snapshotting existed have null here — skip rather than zero,
  // since a 0 would read as "terrible beach" instead of "not recorded".
  const points = useMemo(() => {
    if (!readings) return []
    return readings
      .filter((r) => r.index_at_ingest !== null && r.index_at_ingest !== undefined)
      .map((r) => ({ t: new Date(r.taken_at).getTime(), v: r.index_at_ingest, raw: r }))
      .sort((a, b) => a.t - b.t)
  }, [readings])

  if (error) return <p className="state state--error">Couldn’t load history — {error}</p>
  if (readings === null) return <p className="state">Loading history…</p>

  if (points.length === 0) {
    return (
      <p className="timeline__empty">
        No index history recorded yet. Each ingest stores one point, so the chart fills in
        as the hourly poller runs.
      </p>
    )
  }

  if (points.length === 1) {
    const only = points[0]
    return (
      <p className="timeline__empty">
        Only one reading so far — <strong>{formatScore(only.v)}</strong> at{' '}
        {new Date(only.t).toLocaleString()}. A line needs at least two points.
      </p>
    )
  }

  const t0 = points[0].t
  const t1 = points[points.length - 1].t
  const span = t1 - t0 || 1
  const x = (t) => PAD.left + ((t - t0) / span) * (W - PAD.left - PAD.right)
  const y = (v) => PAD.top + (1 - v / 10) * (H - PAD.top - PAD.bottom)

  // Break the path at gaps rather than interpolating across them.
  const segments = []
  let current = [points[0]]
  for (let i = 1; i < points.length; i += 1) {
    const gapH = (points[i].t - points[i - 1].t) / 3_600_000
    if (gapH > GAP_HOURS) {
      segments.push(current)
      current = [points[i]]
    } else {
      current.push(points[i])
    }
  }
  segments.push(current)

  const fmtTime = (t) =>
    new Date(t).toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    })

  return (
    <div className="timeline">
      <svg
        className="timeline__svg"
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Beach index over time, ${points.length} readings`}
      >
        {/* Band stripes: the chart's own y-axis colored by the same five bands,
            so a point's height and its color reinforce each other. */}
        {SCORE_BANDS.map((b) => (
          <rect
            key={b.label}
            x={PAD.left}
            y={y(b.max)}
            width={W - PAD.left - PAD.right}
            height={y(b.min) - y(b.max)}
            fill={b.color}
            opacity="0.07"
          />
        ))}

        {[0, 5, 10].map((v) => (
          <g key={v}>
            <line
              x1={PAD.left}
              y1={y(v)}
              x2={W - PAD.right}
              y2={y(v)}
              stroke="var(--rule)"
              strokeWidth="1"
            />
            <text className="timeline__axis" x={PAD.left - 7} y={y(v) + 3} textAnchor="end">
              {v}
            </text>
          </g>
        ))}

        {segments.map((seg, i) =>
          seg.length > 1 ? (
            <polyline
              key={i}
              fill="none"
              stroke="var(--ink-soft)"
              strokeWidth="1.5"
              strokeLinejoin="round"
              points={seg.map((p) => `${x(p.t)},${y(p.v)}`).join(' ')}
            />
          ) : null,
        )}

        {points.map((p) => (
          <circle
            key={p.t}
            cx={x(p.t)}
            cy={y(p.v)}
            r={hover?.t === p.t ? 6 : 4}
            fill={scoreToColor(p.v)}
            stroke="var(--surface)"
            strokeWidth="1.5"
            onMouseEnter={() => setHover(p)}
            onMouseLeave={() => setHover(null)}
          />
        ))}

        {hover && (
          <line
            x1={x(hover.t)}
            y1={PAD.top}
            x2={x(hover.t)}
            y2={H - PAD.bottom}
            stroke="var(--ink-faint)"
            strokeWidth="1"
            strokeDasharray="3 3"
          />
        )}

        <text className="timeline__axis" x={PAD.left} y={H - 8}>
          {fmtTime(t0)}
        </text>
        <text className="timeline__axis" x={W - PAD.right} y={H - 8} textAnchor="end">
          {fmtTime(t1)}
        </text>
      </svg>

      <div className="timeline__readout">
        {hover ? (
          <>
            <strong style={{ color: scoreToColor(hover.v) }}>
              {formatScore(hover.v)} · {scoreToLabel(hover.v)}
            </strong>
            <span>{fmtTime(hover.t)}</span>
            {hover.raw.crowd_count != null && <span>{hover.raw.crowd_count} people</span>}
            {hover.raw.coverage_pct != null && (
              <span>{hover.raw.coverage_pct.toFixed(2)}% sargassum</span>
            )}
          </>
        ) : (
          <span className="timeline__hint">
            {points.length} readings · hover a point
            {segments.length > 1 && ` · ${segments.length - 1} overnight gap${segments.length > 2 ? 's' : ''}`}
          </span>
        )}
      </div>
    </div>
  )
}
