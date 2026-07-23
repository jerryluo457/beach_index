import { useNavigate } from 'react-router-dom'
import Speedometer from './Speedometer.jsx'
import { formatUvIndex, scoreToColor, scoreToLabel } from '../score.js'

/**
 * BeachCard — one rectangular panel: gauge on the left third, name + stats on
 * the right two-thirds. The whole rectangle is the click target for
 * /beach/:id, so it's a <button> rather than a div with an onClick — that
 * gets keyboard activation and focus for free.
 *
 * Every stat value is nowrap + ellipsis (see .stats dd) so a long value
 * degrades to "UV Index: Very Hi…" instead of clipping mid-glyph or pushing
 * past the card border.
 */

const fmtTemp = (v) => (v === null || v === undefined ? null : `${Math.round(v)}°F`)

function Stat({ label, value, beta = false }) {
  const absent = value === null || value === undefined || value === ''
  return (
    <>
      <dt>
        {label}
        {beta && <span className="beta-tag">beta</span>}
      </dt>
      <dd className={absent ? 'is-absent' : undefined} title={absent ? undefined : String(value)}>
        {absent ? '—' : value}
      </dd>
    </>
  )
}

export default function BeachCard({ beach, index = 0, onHover }) {
  const navigate = useNavigate()
  const color = scoreToColor(beach.index)

  return (
    <button
      type="button"
      className="card"
      style={{ '--band': color, animationDelay: `${index * 90}ms` }}
      onClick={() => navigate(`/beach/${beach.id}`)}
      onMouseEnter={() => onHover?.(beach.id)}
      onMouseLeave={() => onHover?.(null)}
      onFocus={() => onHover?.(beach.id)}
      onBlur={() => onHover?.(null)}
      aria-label={`${beach.name}, beach index ${beach.index ?? 'unavailable'}. Open detail view.`}
    >
      <div className="card__gauge">
        <Speedometer score={beach.index} size={118} valueSize={23} labelSize={9} />
      </div>

      <div className="card__body">
        {/* Forecast text lives on the detail page, not here — the card has to
            hold a 2:1 rectangle, and a seventh line of copy blows that out. */}
        <h2 className="card__name" title={beach.name}>
          {beach.name}
        </h2>

        <dl className="stats">
          <Stat label="Temp" value={fmtTemp(beach.temp_f)} />
          <Stat label="Water Temp" value={fmtTemp(beach.water_temp_f)} />
          {/* The MEASURED index, not subscores.uv — see score.js for why the
              sub-score can't be inverted back into an index. */}
          <Stat label="UV Index" value={formatUvIndex(beach.uv_index)} />
          <Stat label="Tide" value={beach.next_tide} />
          {/* Sargassum and water only exist on the two validated cameras; the
              other two beaches print an explicit "Not supported" rather than a
              blank, so an absent signal never reads as a good one. */}
          <Stat
            label="Sargassum"
            value={beach.supported ? beach.sargassum_label : 'Unsupported'}
          />
          <Stat
            label="Water"
            value={
              beach.subscores?.water === null || beach.subscores?.water === undefined
                ? 'Unsupported'
                : scoreToLabel(beach.subscores.water)
            }
            beta
          />
        </dl>
      </div>
    </button>
  )
}
