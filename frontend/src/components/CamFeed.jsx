import { useState } from 'react'
import { mediaUrl } from '../api.js'

/**
 * CamFeed — the latest ingested frame with model overlays.
 *
 * TWO DIFFERENT SECTIONS, depending on the beach:
 *
 *  - Lake Worth / Boynton have cameras the models are validated on, so they get
 *    the stored frame plus toggleable sargassum-mask and person-detection
 *    layers.
 *  - Jupiter / Boca have no supported camera. Rather than render an empty or
 *    permanently-disabled panel, they link out to the county's own cam page —
 *    the honest answer is "we don't have this, here's who does".
 *
 * The overlays are transparent PNGs written at ingest time at the frame's own
 * dimensions, so stacking them is pure CSS positioning: no canvas, no
 * client-side compositing, no scaling maths.
 */

const COUNTY_CAMS_URL = 'https://discover.pbc.gov/erm/pages/beach-cams.aspx'

function Toggle({ active, onClick, swatch, children, disabled }) {
  return (
    <button
      type="button"
      className={`camfeed__toggle${active ? ' is-active' : ''}`}
      onClick={onClick}
      disabled={disabled}
      aria-pressed={active}
    >
      <span className="camfeed__swatch" style={{ background: swatch }} />
      {children}
    </button>
  )
}

export default function CamFeed({ beach }) {
  const [showSargassum, setShowSargassum] = useState(true)
  const [showCrowd, setShowCrowd] = useState(false)

  // Unsupported beaches: point at the county feed instead.
  if (!beach.supported) {
    return (
      <div className="camfeed camfeed--external">
        <p className="camfeed__external-copy">
          No analysed camera for {beach.name}. The sargassum and crowd models are only
          validated on the Lake Worth and Boynton inlet cameras.
        </p>
        <a
          className="camfeed__external-link"
          href={COUNTY_CAMS_URL}
          target="_blank"
          rel="noopener noreferrer"
        >
          Palm Beach County beach cams ↗
        </a>
      </div>
    )
  }

  if (!beach.frame_url) {
    return (
      <div className="camfeed camfeed--empty">
        <p className="camfeed__external-copy">
          No frame ingested yet for {beach.name}. The hourly poller stores one during
          daylight hours, or you can submit one from the ingest console.
        </p>
      </div>
    )
  }

  return (
    <div className="camfeed">
      <div className="camfeed__stack">
        <img className="camfeed__base" src={mediaUrl(beach.frame_url)} alt={`Latest cam frame at ${beach.name}`} />
        {showSargassum && beach.sargassum_mask_url && (
          <img
            className="camfeed__layer"
            src={mediaUrl(beach.sargassum_mask_url)}
            alt="Sargassum detected by the segmentation model, shaded in orange"
          />
        )}
        {showCrowd && beach.crowd_overlay_url && (
          <img
            className="camfeed__layer"
            src={mediaUrl(beach.crowd_overlay_url)}
            alt="Boxes around each person detected by the crowd counter"
          />
        )}
      </div>

      <div className="camfeed__controls">
        <Toggle
          active={showSargassum}
          onClick={() => setShowSargassum((v) => !v)}
          swatch="#d25a28"
          disabled={!beach.sargassum_mask_url}
        >
          Sargassum
          {beach.coverage_pct != null && (
            <span className="camfeed__stat">{beach.coverage_pct.toFixed(2)}%</span>
          )}
        </Toggle>

        <Toggle
          active={showCrowd}
          onClick={() => setShowCrowd((v) => !v)}
          swatch="#3cf0ff"
          disabled={!beach.crowd_overlay_url}
        >
          People
          {beach.crowd_count != null && (
            <span className="camfeed__stat">{beach.crowd_count}</span>
          )}
        </Toggle>

        <span className="camfeed__meta">
          {beach.updated_at ? new Date(beach.updated_at).toLocaleString() : ''}
        </span>
      </div>

      <p className="camfeed__caption">
        Model output, not ground truth. The crowd counter misses people near the horizon
        where there is no detail left to detect, so treat it as a density estimate.
      </p>
    </div>
  )
}
