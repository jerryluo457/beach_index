# Beach Index — Palm Beach County

A local-first beach conditions dashboard for four Palm Beach County beaches. It
scrapes public beach cam frames, runs three computer-vision models over them
(sargassum coverage, water severity, crowd count), blends the results with live
weather, tide, rip-current and UV data, and serves a single 0-10 **Beach Quality
Index** per beach through a React dashboard.

Beaches covered: Jupiter Inlet, Lake Worth Inlet, Boynton Inlet, and Boca Raton
(Spanish River Park).

---

## Table of contents

- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Cloning the repository (Git LFS)](#cloning-the-repository-git-lfs)
- [Backend setup](#backend-setup)
- [Frontend setup](#frontend-setup)
- [Running the app](#running-the-app)
- [API reference](#api-reference)
- [Ingesting frames](#ingesting-frames)
- [The hourly poller](#the-hourly-poller)
- [Tests](#tests)
- [Build process](#build-process)
- [Docker](#docker)
- [Configuration reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)

---

## Architecture

The system is a hybrid of expensive-but-slow-moving signals and cheap-but-fast-
moving ones. That split drives the whole design:

| Signal | Cost | Cadence | Where it runs |
| --- | --- | --- | --- |
| Sargassum coverage, crowd count, water severity | Seconds of CPU per frame | Hours to days | At **ingest**, stored in SQLite |
| Weather, tide, water temp, UV, rip risk | One cached HTTP call | ~15 minutes | **Per request**, live |

Combining a stale weather reading with a fresh sargassum reading (or the
reverse) would make one of the two wrong, so the index is **assembled at request
time** from stored ML output plus live external data.

Backend module boundaries are deliberate and enforced by convention:

```
main.py        HTTP only: routes, request/response shapes, model lifecycle.
               No model math, no scoring formulas.
inference.py   Image in, prediction out. Knows PyTorch. Knows nothing about
               beaches or HTTP.
scoring.py     Pure functions. Sub-scores plus the weighted geometric-mean
               aggregate. No I/O.
sources.py     External APIs (NWS, NOAA CO-OPS, Open-Meteo, EPA). Returns
               plain dataclasses, or None on failure.
summarize.py   Local LLM that phrases the already-computed facts as a
               paragraph. Optional, never blocks a response.
poller.py      Scrapes the newest cam frame and pushes it through the same
               run_ingest() path a manual upload takes.
```

On the frontend, `src/api.js` is the single network boundary. Components import
functions, never URLs. A `MOCK` flag in that file runs the entire dashboard off
fixtures with no Python process at all.

### The index

`scoring.py` computes six sub-scores on a 0-10 scale (higher is better) and
combines them with a weighted geometric mean:

| Sub-score | Weight | Notes |
| --- | --- | --- |
| `weather` | 0.28 | Temp, humidity, precipitation probability, wind |
| `sargassum` | 0.20 | Only on cameras the model is validated for |
| `rip` | 0.16 | Safety signal from the NWS Surf Zone Forecast |
| `water` | 0.14 | Beta classifier, weighted down accordingly |
| `crowd` | 0.12 | Beta YOLO + SAHI people count |
| `uv` | 0.10 | Sunburn risk |

Any sub-score can be `None` when its signal is unavailable; the aggregate
renormalizes the remaining weights around the gap rather than failing.

The sargassum model is only trusted on **Lake Worth** and **Boynton** (see
`SARGASSUM_SUPPORTED` in `backend/main.py`). Jupiter and Boca are a different
camera viewpoint where the model degrades, so their sargassum sub-score is
reported as `None` rather than as a low-confidence number.

---

## Repository layout

```
beach_index_app_local/
├── README.md
├── design.txt                  Original design notes
├── .gitattributes              Git LFS tracking rules for model weights
├── .gitignore
│
├── frontend/                   React 18 + Vite single-page app
│   ├── package.json
│   ├── vite.config.js          Dev server on :5173, proxies /api to :8000
│   ├── eslint.config.js
│   ├── index.html
│   ├── public/
│   └── src/
│       ├── main.jsx            Entry point
│       ├── App.jsx             Routes: /, /beach/:id, /upload
│       ├── api.js              All backend calls plus the MOCK flag
│       ├── score.js            0-10 to label/color bands (single source of truth)
│       ├── score.test.js       Node test runner suite for score.js
│       ├── index.css
│       ├── pages/
│       │   ├── Home.jsx        Banner, ranked cards, map
│       │   ├── BeachDetail.jsx Single-beach deep view
│       │   └── Upload.jsx      Admin-only manual frame upload
│       └── components/
│           ├── RecommendationBanner.jsx
│           ├── BeachCard.jsx
│           ├── BeachMap.jsx    MapLibre, markers synced to cards
│           ├── CamFeed.jsx     Frame plus mask/box overlays
│           ├── Speedometer.jsx
│           └── ForecastTimeline.jsx
│
└── backend/                    FastAPI inference and scoring API
    ├── main.py                 App, endpoints, lifespan model loading
    ├── inference.py            U-Net segmenter, water classifier, crowd counter
    ├── scoring.py              Index math
    ├── sources.py              NWS / NOAA / Open-Meteo / EPA fetchers
    ├── summarize.py            Local LLM summary with a hallucination validator
    ├── poller.py               Hourly cam scraper
    ├── test_sources.py
    ├── test_summarize.py
    ├── requirements.txt
    ├── Dockerfile
    ├── com.sargassum.poller.plist   launchd job for the poller (not installed)
    ├── models/                 Tracked in Git LFS
    │   ├── best.pt             Sargassum segmenter (U-Net, IoU 0.33)  ~98 MB
    │   ├── water_best.pt       Water severity classifier (beta)       ~85 MB
    │   └── yolo26m.pt          Crowd counter base weights             ~44 MB
    └── data/                   Runtime only, not committed
        ├── readings.db         SQLite database, created on first boot
        ├── uploads/            Stored frames and overlay PNGs
        └── poller.log
```

---

## Prerequisites

| Tool | Version used | Notes |
| --- | --- | --- |
| Python | 3.11 - 3.13 | 3.13.9 locally; the Dockerfile pins 3.11 |
| Node.js | 22.x | 22.17.0 locally; Vite 8 requires Node 20.19+ |
| npm | 10.x | Ships with Node 22 |
| Git LFS | 3.x | **Required** — the `.pt` weights are LFS objects |

Roughly 3 GB of disk is needed for the Python environment (PyTorch dominates),
plus about 230 MB for the model weights and about 500 MB for the summarizer
model that Hugging Face caches on first run.

No API keys are needed. Every external data source used (NWS, NOAA CO-OPS,
Open-Meteo, EPA) is free and unauthenticated. NWS asks for an identifying
`User-Agent`, set in `backend/sources.py`.

---

## Cloning the repository (Git LFS)

The three `.pt` files are stored in Git LFS. Install LFS **before** cloning, or
you will get 130-byte pointer text files where the weights should be.

```bash
git lfs install
```

```bash
git clone https://github.com/jerryluo457/beach_index_app_local.git
```

```bash
cd beach_index_app_local && git lfs pull
```

Verify the weights actually downloaded — each file should be tens of megabytes,
not a few hundred bytes:

```bash
ls -lh backend/models
```

If they came down as pointers (a file whose contents start with
`version https://git-lfs.github.com/spec/v1`), run `git lfs install` and then
`git lfs pull` again.

---

## Backend setup

All backend commands assume you are in the `backend/` directory. This matters:
`main.py` resolves `data/` and `models/` relative to the process working
directory, so starting the server from the repository root will create an empty
database in the wrong place and fail to find the weights.

Create and activate a virtual environment:

```bash
cd backend && python3 -m venv .venv && source .venv/bin/activate
```

Install dependencies:

```bash
pip install --upgrade pip && pip install -r requirements.txt
```

This installs PyTorch, Ultralytics, SAHI, `segmentation_models_pytorch`,
Transformers, FastAPI and friends. Expect a few minutes and about 3 GB.

Start the API:

```bash
uvicorn main:app --reload --port 8000
```

On boot you should see each model report in:

```
[models]   sargassum model loaded
[models]   water model loaded (beta)
[models]   crowd model loaded (beta)
[models]   summarizer loaded
```

A model that fails to load prints `FAILED` and is skipped — the app degrades
rather than refusing to start. Confirm the state at any time with the health
endpoint, which lists exactly which models are live:

```bash
curl http://127.0.0.1:8000/
```

Interactive API docs are at http://127.0.0.1:8000/docs.

The first request that needs a summary downloads `Qwen/Qwen2.5-0.5B-Instruct`
(about 500 MB) into the Hugging Face cache. After that it runs fully offline. If
the download fails, summaries fall back to a deterministic template and nothing
else is affected.

---

## Frontend setup

From the repository root:

```bash
cd frontend && npm install
```

Start the dev server:

```bash
npm run dev
```

The app is at http://localhost:5173. Vite proxies `/api/*` to
`http://127.0.0.1:8000` (see `frontend/vite.config.js`), so `src/api.js` uses
relative paths only and needs no environment branching between dev and a
reverse-proxied production deployment.

### Running the frontend without the backend

Set `MOCK = true` at the top of `frontend/src/api.js`. The whole dashboard then
runs off fixtures that mirror the backend's `BeachSummary` and `IngestResult`
shapes field-for-field, with no Python process running.

---

## Running the app

Two terminals, in this order.

Terminal 1 — API:

```bash
cd backend && source .venv/bin/activate && uvicorn main:app --reload --port 8000
```

Terminal 2 — dashboard:

```bash
cd frontend && npm run dev
```

Then open http://localhost:5173.

On a completely fresh clone the database is empty, so every beach shows weather,
tide, UV and rip data but no sargassum or crowd numbers. Populate it by running
the poller once or by uploading a frame — see the next two sections.

---

## API reference

Base URL in development: `http://127.0.0.1:8000`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Health check. Reports which models loaded and which beaches exist. |
| `GET` | `/beaches` | Every beach with its current index. Backs the home page. |
| `GET` | `/beach/{beach_id}` | One beach in full. Backs `/beach/:id`. 404 on unknown id. |
| `POST` | `/ingest/{beach_id}` | Multipart upload of a cam frame. Runs all three models, stores the reading. Synchronous, several seconds. |
| `POST` | `/poll/{beach_id}` | Scrape the newest cam frame now, the same way the hourly job does. Backs the "Refresh" link. 404 for beaches with no camera. Synchronous, several seconds. |
| `GET` | `/history/{beach_id}?limit=30` | Past readings, oldest first. Backs the forecast timeline. |
| `GET` | `/media/{filename}` | Static mount over `data/uploads` — stored frames and overlay PNGs. |

Valid `beach_id` values: `lake-worth`, `boynton`, `jupiter`, `boca`.

---

## Ingesting frames

Through the UI: open http://localhost:5173/upload, pick a beach, choose a JPEG.
The page is deliberately not linked from the main nav — it is an admin tool.

Through the API:

```bash
curl -F "file=@../data/test_photo.jpg" http://127.0.0.1:8000/ingest/lake-worth
```

Directly against the models, with no server or database involved — useful when
debugging inference itself:

```bash
cd backend && python inference.py ../data/test_photo.jpg all
```

The third argument selects which model to run (`all` runs every one).

Every ingested frame is downscaled to 1200 px wide before inference. This is a
correctness requirement, not a speed knob: the crowd counter slices with SAHI at
512 px, so the tile count — and therefore how large a person appears within a
tile — scales with the input. Changing `INGEST_WIDTH` in `backend/main.py`
invalidates comparisons against every reading already stored.

---

## The hourly poller

`poller.py` scrapes the newest published frame for the two beaches that have
cameras and pushes it through the same `run_ingest()` path a manual upload
takes.

```bash
cd backend && python poller.py
```

```bash
python poller.py lake-worth
```

```bash
python poller.py --force
```

`--force` ignores both the daylight window and the "already processed this
frame" dedup check.

The cam site has no API — the poller parses a plain-text slideshow manifest, and
that format can change without notice. Every step fails loudly into the log and
leaves the last good reading untouched, so a broken scrape degrades to stale
data rather than to a crash.

### Scheduling it on macOS

`com.sargassum.poller.plist` is a launchd job that runs the poller hourly from
06:05 to 19:05 (the cameras publish nothing overnight). It is **not** installed
automatically, and its paths are absolute — edit them to match your checkout
before loading it.

```bash
cp backend/com.sargassum.poller.plist ~/Library/LaunchAgents/
```

```bash
launchctl load ~/Library/LaunchAgents/com.sargassum.poller.plist
```

```bash
launchctl list | grep sargassum
```

```bash
launchctl unload ~/Library/LaunchAgents/com.sargassum.poller.plist
```

The job's `WorkingDirectory` must be `backend/` for the same reason you start
uvicorn from there.

The middle column of `launchctl list | grep sargassum` is the last exit status.
It must be `0`. A **78** there means launchd could not spawn the interpreter —
almost always because the project moved and the installed plist still points at
the old path. Every path in the plist is absolute, so **renaming or moving the
project silently kills the poller**: launchd keeps the job loaded, writes a
zero-byte log at the dead path, and tells nobody. The dashboard goes on serving
the last stored reading, which looks exactly like a working poller until you
compare the cam panel against the camera's own page. After any move, re-copy the
plist, unload/load it, and check that status column.

Two things now make that failure visible rather than silent:

- Any reading older than two poll cycles is labelled **stale** in amber, and any
  reading not from today carries its date, in the masthead and the cam panel.
- The **Refresh** link beside those timestamps calls `POST /poll/{beach_id}` and
  scrapes on demand, so recovering does not require a terminal.

---

## Tests

Frontend — the scoring bands (`score.test.js`) and the ingest-staleness helper
(`api.test.js`), run under the Node test runner:

```bash
cd frontend && npm test
```

Frontend lint:

```bash
cd frontend && npm run lint
```

Backend — external-source parsing, including the falsy-zero regression (a real
UV reading of 0 after sunset must not be treated as missing data):

```bash
cd backend && python test_sources.py
```

Backend — the summarizer's hallucination validator, built from actual failures
observed across four candidate local models:

```bash
cd backend && python test_summarize.py
```

`test_sources.py` makes live calls to NWS and NOAA, so it needs network access
and can fail transiently if either service is down.

---

## Build process

### Frontend production build

```bash
cd frontend && npm run build
```

Vite type-checks nothing (this is plain JSX), bundles through Rollup, and writes
hashed assets to `frontend/dist/`:

```
dist/
├── index.html
└── assets/
    ├── index-<hash>.css
    ├── index-<hash>.js
    └── maplibre-gl-<hash>.js
```

MapLibre is emitted as its own chunk because it is large and changes far less
often than app code, so it stays cached across deploys.

Preview the built bundle exactly as it will be served:

```bash
cd frontend && npm run preview
```

Note that `npm run preview` does **not** apply the dev proxy. To point a built
bundle at a backend, set `VITE_API_BASE` at build time:

```bash
cd frontend && VITE_API_BASE=https://your-api.example.com npm run build
```

`src/api.js` reads `import.meta.env.VITE_API_BASE` and falls back to `/api`,
which is the right default when the bundle is served behind a reverse proxy that
forwards `/api` to the FastAPI process.

### Backend

There is no compile step. The "build" is the virtual environment plus the model
weights, or the Docker image below.

---

## Docker

The image covers the backend only. Build from inside `backend/`, because the
Dockerfile copies `requirements.txt` and `models/` from the build context root:

```bash
cd backend && docker build -t beach-index-api .
```

```bash
docker run -p 8000:8000 -v "$(pwd)/data:/data" beach-index-api
```

The image is single-stage and runs `uvicorn main:app --host 0.0.0.0 --port 8000
--workers 2`. Two workers is why readings go to SQLite rather than a module-level
dict: each worker process would get its own copy of a dict, so an `/ingest`
handled by worker 1 would be invisible to a `/beaches` served by worker 2. A
SQLite file is shared across both.

`requirements.txt` is copied and installed before the rest of the source, so
edits to Python files reuse the cached dependency layer.

If you deploy this anywhere real, add the deployed frontend origin to the
`CORSMiddleware` `allow_origins` list in `backend/main.py`; only
`http://localhost:5173` is allowed today. Symptom of forgetting: `/docs` and
`curl` work fine while the browser reports a CORS block, because that check is
enforced by the browser and not the server.

---

## Configuration reference

| Setting | File | Default | Purpose |
| --- | --- | --- | --- |
| `MOCK` | `frontend/src/api.js` | `false` | Run the dashboard off fixtures with no backend |
| `VITE_API_BASE` | environment | `/api` | Backend base URL for a production build |
| `FEATURES` | `frontend/src/api.js` | all `true` | Toggles for cam feed, timeline, LLM summary |
| `SARGASSUM_SUPPORTED` | `backend/main.py` | `lake-worth`, `boynton` | Cameras the segmenter is trusted on |
| `INGEST_WIDTH` | `backend/main.py` | `1200` | Normalization width; changing it invalidates stored crowd counts |
| `DEFAULT_UV` / `DEFAULT_RIP_RISK` | `backend/main.py` | `6.0` / `moderate` | Conservative fallbacks used for scoring when a source is down |
| `DB_PATH` | `backend/main.py` | `data/readings.db` | SQLite location, relative to the working directory |
| `WEIGHTS` | `backend/scoring.py` | see table above | Sub-score weights in the aggregate |
| `NWS_USER_AGENT` | `backend/sources.py` | project contact string | NWS requires an identifying User-Agent |
| `BEACHES` | `backend/sources.py` | four beaches | Coordinates, NOAA tide/temp station ids, ZIP codes |
| `MODEL_NAME` | `backend/summarize.py` | `Qwen/Qwen2.5-0.5B-Instruct` | Local summarization model |

`backend/sources.py` carries extensive notes on why several beaches share a tide
station: nearly every nearby CO-OPS station sits on the Intracoastal Waterway,
which lags the open ocean by roughly two hours. These beaches face the ocean, so
a closer ICWW station would look more local while describing the wrong body of
water.

---

## Troubleshooting

**Model weights are a few hundred bytes.** Git LFS was not installed before
cloning. Run `git lfs install` then `git lfs pull`.

**`[models] sargassum model FAILED` on startup.** Almost always the working
directory. Start uvicorn from `backend/`, not from the repository root.

**Every beach shows an index but no sargassum or crowd values.** The database
has no readings yet. Run `python poller.py --force` or upload a frame at
`/upload`.

**Jupiter and Boca never show sargassum.** Expected. The model is only validated
on the Lake Worth and Boynton camera viewpoints, so those two report `None` by
design rather than an untrustworthy number.

**Browser reports a CORS block while curl works.** The frontend origin is not in
`allow_origins` in `backend/main.py`.

**Images 404 in dev.** `mediaUrl()` in `api.js` prefixes media paths with the
same base every fetch uses. Without the prefix the browser asks Vite (:5173) for
`/media/...` and receives the SPA's `index.html` instead of an image.

**`/ingest` takes several seconds.** Expected. Sliced crowd inference is the
cost, and the endpoint is synchronous on purpose so failures surface immediately
instead of in a log. For user-facing use, move it to FastAPI `BackgroundTasks`
and return 202.

**No summary paragraph appears.** The summarizer is optional. If the Hugging
Face download failed or the generated text was rejected by the validator in
`summarize.py`, the app omits the paragraph and shows everything else. Safety
information such as the rip current advisory is rendered separately by the
frontend and never depends on that model.

---

## Notes and limitations

- The sargassum segmenter reports IoU 0.33 and is a research-grade model, not a
  safety instrument.
- The water severity and crowd models are labelled beta throughout the UI and
  are weighted down in the index accordingly.
- Cam scraping depends on an undocumented plain-text manifest and can break
  without warning.
- Nothing in this project is an authoritative source for beach safety. Check
  official NWS and lifeguard advisories before entering the water.
