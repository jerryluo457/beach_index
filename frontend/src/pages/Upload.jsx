import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { BEACH_OPTIONS, SARGASSUM_SUPPORTED, ingestBeach } from '../api.js'

/**
 * Upload — /upload. Private admin page for submitting a morning cam frame.
 *
 * This is the app's ONLY write operation. Two details drive the whole design:
 *
 *  1. The beach selector starts unselected. Submitting a frame to the wrong
 *     beach silently corrupts that beach's readings, and a defaulted-to-first
 *     dropdown is exactly how that happens.
 *
 *  2. /ingest runs the U-Net *and* the SAHI crowd counter synchronously —
 *     ten-plus seconds is normal. A bare spinner reads as a hang at that
 *     duration, so the loading state says out loud how long to expect, and
 *     the button is locked for the whole flight so no second request can
 *     stack behind the first.
 */

export default function Upload() {
  const [beachId, setBeachId] = useState('')
  const [file, setFile] = useState(null)
  const [previewUrl, setPreviewUrl] = useState(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const fileInput = useRef(null)

  // Object URLs are a real allocation — revoke the previous one whenever the
  // selection changes, and on unmount.
  useEffect(() => {
    if (!file) {
      setPreviewUrl(null)
      return
    }
    const url = URL.createObjectURL(file)
    setPreviewUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  const canSubmit = Boolean(beachId) && Boolean(file) && !busy

  async function handleSubmit(event) {
    event.preventDefault()
    if (!canSubmit) return

    setBusy(true)
    setResult(null)
    setError(null)

    try {
      const data = await ingestBeach(beachId, file)
      setResult(data)
    } catch (err) {
      setError(err.message || 'Request failed')
    } finally {
      setBusy(false)
    }
  }

  const beachName = (id) => BEACH_OPTIONS.find((b) => b.id === id)?.name ?? id

  return (
    <div className="utility">
      <Link to="/" className="back-link">← Dashboard</Link>

      <header style={{ padding: '18px 0 26px' }}>
        <p className="masthead__eyebrow">Admin · not linked for visitors</p>
        <h1 className="masthead__title" style={{ fontSize: 42 }}>
          Ingest <em>console</em>
        </h1>
      </header>

      <form className="panel" onSubmit={handleSubmit}>
        <div className="field">
          <label className="field__label" htmlFor="beach">
            Beach
          </label>
          <select
            id="beach"
            className="control"
            value={beachId}
            disabled={busy}
            onChange={(e) => setBeachId(e.target.value)}
          >
            <option value="">— Select a beach —</option>
            {BEACH_OPTIONS.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </select>
          {beachId && !SARGASSUM_SUPPORTED.includes(beachId) && (
            <p className="field__hint">
              Sargassum model is not validated here — crowd and water only.
            </p>
          )}
        </div>

        <div className="field">
          <label className="field__label" htmlFor="frame">
            Cam frame
          </label>
          <input
            id="frame"
            ref={fileInput}
            className="control"
            type="file"
            accept="image/*"
            disabled={busy}
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />

          {previewUrl && (
            <div className="preview">
              <img src={previewUrl} alt="Selected frame preview" />
              <div className="preview__meta">
                <strong>{file.name}</strong>
                {(file.size / 1024 / 1024).toFixed(2)} MB · {file.type || 'unknown type'}
              </div>
            </div>
          )}
        </div>

        <button type="submit" className="btn" disabled={!canSubmit}>
          {busy && <span className="spinner" />}
          {busy ? 'Analyzing…' : 'Run analysis'}
        </button>

        {busy && (
          <div className="notice notice--working">
            <div className="notice__title">Working</div>
            <p className="notice__body">
              Analyzing photo — this can take up to 10–15 seconds. The sargassum segmenter and the
              SAHI-sliced crowd counter both run before the response returns. Don’t reload the page.
            </p>
          </div>
        )}

        {error && (
          <div className="notice notice--error">
            <div className="notice__title">Ingest failed</div>
            <p className="notice__body">
              Nothing was stored. Server said: <code>{error}</code>
            </p>
          </div>
        )}

        {result && (
          <div className="notice notice--ok">
            <div className="notice__title">Stored · now live on the dashboard</div>
            <dl className="result-rows">
              <dt>Beach</dt>
              <dd>{beachName(result.beach)}</dd>

              <dt>Sargassum</dt>
              <dd>
                {result.coverage_pct === null || result.coverage_pct === undefined
                  ? 'Not supported for this beach'
                  : `${result.coverage_pct.toFixed(1)}% · ${result.sargassum_label ?? '—'}`}
              </dd>

              <dt>Crowd</dt>
              <dd>
                {result.crowd_count === null || result.crowd_count === undefined
                  ? '—'
                  : `${result.crowd_count} people`}
              </dd>

              <dt>
                Water<span className="beta-tag">beta</span>
              </dt>
              <dd>{result.water_severity ?? '—'}</dd>

              <dt>Stored at</dt>
              <dd>{new Date(result.stored_at).toLocaleString()}</dd>
            </dl>
            <p className="notice__body" style={{ marginTop: 12 }}>
              <Link to={`/beach/${result.beach}`} style={{ borderBottom: '1px solid currentColor' }}>
                Open {beachName(result.beach)} →
              </Link>
            </p>
          </div>
        )}
      </form>
    </div>
  )
}
