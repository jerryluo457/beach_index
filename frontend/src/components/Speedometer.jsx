import { useEffect, useState } from 'react'
import {
  SCORE_BANDS,
  SCORE_MAX,
  formatScore,
  scoreToAngle,
  scoreToBand,
} from '../score.js'

/**
 * Speedometer — a genuine 180° automotive-style gauge.
 *
 * GEOMETRY. The arc is drawn on a circle centered at (CX, CY) with the flat
 * side down: 180° sits at 9 o'clock (score 0), 0° at 3 o'clock (score 10), and
 * the sweep passes over the top through 90° (score 5). A point at angle θ is
 *
 *     x = CX + R·cos θ        y = CY − R·sin θ
 *
 * so y decreases as we rise over the top, which is what SVG's downward y-axis
 * requires.
 *
 * The arc is NOT a gradient — it is five discrete <path> segments generated
 * directly from SCORE_BANDS in score.js. Adding a band to that table adds a
 * segment here with no other edit, which is the whole point of keeping the
 * thresholds in one place.
 *
 * The needle is continuous: it points at the exact score, so it can land
 * anywhere, but it always lands inside one of the five painted regions.
 */

const CX = 100
const CY = 100
const R = 76
const ARC_WIDTH = 15
const GAP_DEG = 1.1 // hairline of paper between adjacent bands

function polar(angleDeg, radius = R) {
  const rad = (angleDeg * Math.PI) / 180
  return {
    x: CX + radius * Math.cos(rad),
    y: CY - radius * Math.sin(rad),
  }
}

/** Arc path from score `from` to score `to`, along the gauge circle. */
function bandPath(from, to) {
  // Score increases left→right, but the angle DEcreases (180° → 0°).
  const startAngle = 180 - (from / SCORE_MAX) * 180 - GAP_DEG
  const endAngle = 180 - (to / SCORE_MAX) * 180 + GAP_DEG
  const a = polar(startAngle)
  const b = polar(endAngle)
  // sweep-flag 1 = clockwise in SVG's coordinate system, which is the direction
  // of increasing score here. All bands are well under 180°, so large-arc = 0.
  return `M ${a.x.toFixed(2)} ${a.y.toFixed(2)} A ${R} ${R} 0 0 1 ${b.x.toFixed(2)} ${b.y.toFixed(2)}`
}

export default function Speedometer({
  score,
  size = 152,
  showValue = true,
  showLabel = true,
  showTicks = true,
  valueSize = 26,
  labelSize = 10,
}) {
  const band = scoreToBand(score)
  const hasScore = score !== null && score !== undefined && !Number.isNaN(Number(score))
  const target = hasScore ? scoreToAngle(score) : 0

  // Mount at zero, then sweep to the real value one frame later, so the needle
  // animates in like an instrument powering on.
  const [angle, setAngle] = useState(0)
  useEffect(() => {
    const id = setTimeout(() => setAngle(target), 60)
    return () => clearTimeout(id)
  }, [target])

  // The needle blade is authored pointing LEFT — θ = 180°, which is score 0 —
  // so the rotation IS the sweep amount: 0° at score 0, 180° at score 10.
  // Rotation is clockwise-positive on screen, and increasing score moves
  // clockwise along the arc, so the two agree with no sign flip.
  const rotation = angle

  const majorTicks = [0, 2, 4, 6, 8, 10]

  return (
    <div className="gauge">
      <svg
        className="gauge__svg"
        width={size}
        height={size * 0.62}
        viewBox="0 0 200 124"
        role="img"
        aria-label={
          hasScore
            ? `Beach index ${formatScore(score)} out of 10 — ${band.label}`
            : 'Beach index unavailable'
        }
      >
        {/* Unlit trough behind the bands, so a missing score still reads as a gauge */}
        <path
          d={bandPath(0, 10)}
          fill="none"
          stroke="var(--surface-sunk)"
          strokeWidth={ARC_WIDTH + 4}
          strokeLinecap="butt"
        />

        {SCORE_BANDS.map((b) => (
          <path
            key={b.label}
            d={bandPath(b.min, b.max)}
            fill="none"
            stroke={b.color}
            strokeWidth={ARC_WIDTH}
            strokeLinecap="butt"
            // All five zones stay legible; the band the needle is in gets full
            // strength so the reading is obvious at card size.
            opacity={hasScore ? (b.label === band.label ? 1 : 0.72) : 0.2}
          />
        ))}

        {showTicks &&
          majorTicks.map((t) => {
            const a = 180 - (t / SCORE_MAX) * 180
            const outer = polar(a, R - ARC_WIDTH / 2 - 1)
            const inner = polar(a, R - ARC_WIDTH / 2 - 6)
            return (
              <line
                key={t}
                x1={outer.x}
                y1={outer.y}
                x2={inner.x}
                y2={inner.y}
                stroke="var(--ink-faint)"
                strokeWidth="1"
              />
            )
          })}

        {/* End labels — orientation cues so 0-left / 10-right is never ambiguous */}
        {showTicks && (
          <>
            <text className="gauge__endcap" x={CX - R} y={CY + 15} textAnchor="middle">
              0
            </text>
            <text className="gauge__endcap" x={CX + R} y={CY + 15} textAnchor="middle">
              10
            </text>
          </>
        )}

        {/* Needle: tapered blade + hub, pivoting on the flat base */}
        <g
          className="gauge__needle"
          style={{
            transform: `rotate(${rotation.toFixed(2)}deg)`,
            transformOrigin: `${CX}px ${CY}px`,
          }}
        >
          <polygon
            points={`${CX + 9},${CY - 4.4} ${CX + 9},${CY + 4.4} ${CX - R + 14},${CY}`}
            fill={hasScore ? 'var(--ink)' : 'var(--ink-faint)'}
          />
        </g>
        <circle cx={CX} cy={CY} r="7" fill="var(--surface)" stroke="var(--ink)" strokeWidth="2" />
        <circle cx={CX} cy={CY} r="2.2" fill="var(--ink)" />

        {/* Baseline closing the flat side of the dial */}
        <line
          x1={CX - R - 6}
          y1={CY}
          x2={CX + R + 6}
          y2={CY}
          stroke="var(--rule)"
          strokeWidth="1"
        />
      </svg>

      {showValue && (
        <div
          className="gauge__value"
          style={{ fontSize: valueSize, color: hasScore ? band.color : 'var(--ink-faint)' }}
        >
          {formatScore(score)}
        </div>
      )}

      {showLabel && (
        <div
          className="gauge__label"
          style={{ fontSize: labelSize, color: hasScore ? band.color : 'var(--ink-faint)' }}
        >
          {band.label}
        </div>
      )}
    </div>
  )
}
