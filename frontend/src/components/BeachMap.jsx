import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import Map, { Marker, NavigationControl, ScaleControl } from 'react-map-gl/maplibre'
import 'maplibre-gl/dist/maplibre-gl.css'
import { SCORE_BANDS, bandRangeLabel, formatScore, scoreToColor, scoreToInk } from '../score.js'

/**
 * BeachMap — MapLibre GL, one circular marker per beach at its real
 * coordinates.
 *
 * Marker fill comes from scoreToColor() and the number's color from
 * scoreToInk() — the same functions the gauges use. That identity is the point:
 * a marker and its card must never disagree about what 8.4 looks like.
 *
 * Basemap is CARTO Positron: a desaturated, key-free style whose gray/paper
 * palette leaves the five band colors as the only saturated marks on the map.
 */

const POSITRON = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json'

// Frame the stations from their own coordinates rather than a hardcoded zoom —
// the map is full-width, so its aspect ratio changes a lot between viewports and
// a fixed zoom either crops Jupiter/Boca off or leaves the coast tiny.
function boundsOf(beaches) {
  const lons = beaches.map((b) => b.lon)
  const lats = beaches.map((b) => b.lat)
  return [
    [Math.min(...lons), Math.min(...lats)],
    [Math.max(...lons), Math.max(...lats)],
  ]
}

// Asymmetric padding: the coastline runs down the middle, so extra room on the
// left keeps the legend from sitting on top of a marker.
const FIT_OPTIONS = { padding: { top: 70, bottom: 70, left: 260, right: 90 } }

export default function BeachMap({ beaches, activeId, onHover }) {
  const navigate = useNavigate()

  const markers = useMemo(
    () =>
      beaches.map((beach) => (
        <Marker
          key={beach.id}
          longitude={beach.lon}
          latitude={beach.lat}
          anchor="center"
          onClick={() => navigate(`/beach/${beach.id}`)}
        >
          <div
            className={`map-marker${activeId === beach.id ? ' is-active' : ''}`}
            style={{
              background: scoreToColor(beach.index),
              color: scoreToInk(beach.index),
            }}
            onMouseEnter={() => onHover?.(beach.id)}
            onMouseLeave={() => onHover?.(null)}
            title={`${beach.name} — ${formatScore(beach.index)}`}
          >
            {formatScore(beach.index)}
            <span className="map-marker__name">{beach.name}</span>
          </div>
        </Marker>
      )),
    [beaches, activeId, navigate, onHover],
  )

  return (
    <div className="map-frame">
      <Map
        initialViewState={{ bounds: boundsOf(beaches), fitBoundsOptions: FIT_OPTIONS }}
        mapStyle={POSITRON}
        style={{ width: '100%', height: '100%' }}
        attributionControl={{ compact: true }}
        dragRotate={false}
        touchPitch={false}
      >
        <NavigationControl position="top-right" showCompass={false} />
        <ScaleControl position="bottom-right" unit="imperial" />
        {markers}
      </Map>

      <div className="map-legend">
        <div className="map-legend__title">Index scale</div>
        {[...SCORE_BANDS].reverse().map((band) => (
          <div className="map-legend__row" key={band.label}>
            <span className="map-legend__swatch" style={{ background: band.color }} />
            {band.label}
            <span className="map-legend__range">{bandRangeLabel(band)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
