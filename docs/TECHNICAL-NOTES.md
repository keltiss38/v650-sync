🇫🇷 [Version française](NOTES-TECHNIQUES.fr.md)

# V650 Sync — technical notes

> Free, unofficial Windows application to use a **Polar V650** (sessions, routes, offline maps).
> Code version: `0.1.0` · Last update of this document: 12 November 2026.
> This document contains no username, password or serial number.
> Independent project, **not affiliated with Polar, Komoot, Strava or Intervals.icu**. Written with the help of Claude (Anthropic).

## Contents

1. [Background](#1-background)
2. [Features and validation level](#2-features-and-validation-level)
3. [Architecture](#3-architecture)
4. [PFTP protocol and device formats](#4-pftp-protocol-and-device-formats)
5. [Routes](#5-routes)
6. [Offline maps](#6-offline-maps)
7. [External integrations](#7-external-integrations)
8. [Local API](#8-local-api)
9. [Log, tests and build](#9-log-tests-and-build)
10. [Measurements and validations](#10-measurements-and-validations)
11. [Problems met](#11-problems-met)
12. [Known limitations and ideas](#12-known-limitations-and-ideas)

---

## 1. Background

The **Polar V650** is a GPS bike computer from 2014. It plugs into a PC with a micro-USB cable and shows up as a **virtual network card (RNDIS)**: no disk drive. The official software, **Polar FlowSync**, depends on Apple's **Bonjour** service to find it; on Windows 10 and 11 this service is often missing or refused, and FlowSync is hardly maintained for this model any more. Bluetooth sync with the mobile app did not work in my case.

Another limit of the original setup: the **offline maps** that FlowSync can install are regional blocks built by Polar in **August 2015** (cartography more than ten years out of date), and the device only accepts the map of a single block at a time.

Project goals: sync without Bonjour or an account, export sessions, manage routes (including Komoot), and **build your own up-to-date maps** (OpenStreetMap), all in a single Windows application.

Scoping decisions: no training analysis (a plain viewer); no firmware modification (it was never modified, nor read with a view to modifying it); no Strava API (paid, and Intervals.icu does not push activities to Strava: a manual upload button instead); a clean design ("bike dashboard").

## 2. Features and validation level

Legend: ✅ validated on the **real device** or the **real service** · 🧪 tested locally (fake server, fake device, local data) · ⏳ not tested.

| Feature | Status | Detail |
|---|---|---|
| mDNS discovery without Bonjour (`zeroconf`) | ✅ | parallel connection on all service/interface pairs, ~3 s |
| Reading files and folders, large files | ✅ | PFTP protocol reconstructed (§4) |
| Full local backup | ✅ | ~300 files, map (~325 MB) skipped by default |
| Status (battery, space), profile, clock | ✅ | |
| Setting the device clock | ✅ | with confirmation |
| Sessions: list, detail, curves, map | ✅ | |
| GPX and FIT export | ✅ | validated against Polar Flow exports (§10) |
| Writing, updating and deleting routes | ✅ | read back byte by byte; projection validated on the device's screen |
| Unified route library (device, Komoot, GPX) | ✅ | "synchronised" status decided on the track |
| **Komoot** (sign-in, list, GPX, writing to the device) | ✅ | real account; "Komoot" origin recognised thanks to the id FlowSync notes in the route |
| **Intervals.icu** (test, manual session upload) | ✅ | real service |
| **Sending a session to Komoot** (FIT) | ✅ | Komoot's internal API (like `kompy.upload_tour`); tested with a fake server, then validated by the author on a real account |
| Automatic upload of new sessions (Intervals, Komoot, GPX) | 🧪 | off by default |
| Map building (Mapsforge v3, OpenStreetMap) | ✅ | maps displayed by the device; drawn area respected |
| Writing a map to the device (multi-frame, read-back, checksum) | ✅ | ~2.2 MB/s |
| Map editor (area, drawing tools, levels of detail) | ✅ | |
| Modal progress window (steps, bars, duration, cancel) | ✅ | |
| Detailed log, bug report | ✅ | |
| `V650Sync.exe` executable (~14 MB, no console, unsigned) | ✅ | Windows 11; **Windows 10 untested** |
| Contour lines on generated maps | ✅ | none / main / full option (SRTM elevations, §6.4); display on the device validated by the author; off by default |
| English and French interface; units (km / miles) following the device's setting | 🧪 | runtime translation (§3.6); checked by an automated sweep of the page, not by a native English reader |
| Update notice (GitHub, once a day, can be turned off) | 🧪 | tested with a fake server; **only works once a release is published** on the public repository (§7) |
| Export of RR intervals, temperature, `device_info` in the FIT | ❌ | absent from the FIT |

## 3. Architecture

### 3.1 Overview

```
 Polar V650            USB cable                     Windows 10/11 PC
 ┌────────────┐   ┌───────────────┐    TCP/IPv6    ┌────────────────────────────────────────┐
 │ PFTP       │◄─►│ virtual       │◄──────────────►│ v650sync (Python)                      │
 │ server     │   │ network card  │  mDNS          │  discover.py · pftp.py · backup.py     │
 │ (protobuf) │   │ RNDIS         │ _polar-ftp._tcp│  service.py · mapgen.py · mapservice.py│
 └────────────┘   └───────────────┘                │  webui.py (HTTP server 127.0.0.1)      │
                                                   │        ▲                               │
                      Native window (WebView2) ◄───┼────────┘  single page index.html       │
                                                   └────────────────────────────────────────┘
                                   HTTPS (depending on your actions): Intervals.icu · Komoot · OSM · Geofabrik · GitHub
```

A **single Python process**: protocol module, local HTTP server (`127.0.0.1` only), one web page displayed in a native window (`pywebview` + WebView2, falling back to an Edge/Chrome "app mode" window, then to the default browser). Everything is packaged into a `.exe` by PyInstaller.

### 3.2 Technology choices

| Choice | Why |
|---|---|
| Python 3, standard library first | a single mandatory dependency (`zeroconf`); readable, auditable code |
| `zeroconf` | `_polar-ftp._tcp` discovery without Bonjour |
| Hand-decoded protobuf | zero dependencies; simple, stable formats |
| Local HTTP server + single HTML page | rich interface (maps, curves) without a heavy toolkit |
| `pywebview` + WebView2 | a real application window, close button can be intercepted for a clean shutdown |
| Home-made FIT encoder (`fit.py`) | no dependency, controlled byte by byte |
| Online OpenStreetMap tiles for previews | no API key; offline, only the track is displayed |
| Maps: Java + Osmosis + `mapsforge-map-writer 0.5.1` downloaded on demand | format imposed by the device (§6); not in the exe (weight) |

### 3.3 Link with the device

1. **USB / RNDIS**: Windows gives the adapter a link-local IPv6 address (`fe80::…`); the device is on the same link.
2. **mDNS / DNS-SD**: the device announces `_polar-ftp._tcp.local.` (name, port, addresses). `discover.find_devices()` listens through `zeroconf`.
3. **Stale records**: the device can announce several services, some with an old dead port. `connect_any()` tries **all (service, interface) pairs in parallel** (up to 48, 2 s timeout) and keeps the first one that sends the PFTP banner. An `fe80::` address only makes sense on a specific interface (a machine has dozens); the interface that worked is remembered.
4. **One client at a time**: a session that is not closed properly leaves the device silent until it is unplugged. Hence `shutdown(SHUT_RDWR)` on close, a global lock (`service.LOCK`) and a **graceful stop** (reading stops between two files, the session is closed, then the server and the window). *Forbidden in tests: killing the server during a read; use `/api/quit`.*

### 3.4 Local server and security

- `ThreadingHTTPServer` bound to `127.0.0.1`, random free port, never exposed to the network.
- `Host` header check (anti DNS rebinding) and a **random token** (`X-Token`) required for every `POST` request.
- No redirect followed towards Komoot; external addresses opened by the application = a **fixed list** (`/api/open-external`).
- **Secrets** (Intervals.icu key, Komoot token) are encrypted with **Windows DPAPI** (`secretstore.py`): decryptable only by the same Windows account on this PC. The Komoot password is never stored. The settings (`config.json`) are shared by a single object across the program and re-read if they change on disk (so a stale copy cannot overwrite a saved key).
- The page never inserts external text without escaping it.

### 3.5 Long tasks

Two independent slots: `main` (operations on the device) and `build` (map building, tool downloads). Each task has a title, **steps** (state, done/total), a progress value, a log and a start time (`jobs.py`). The page follows them across all tabs; building and writing a map open a **blocking window** with one line and one bar per step (tools, each downloaded region, building, check), an overall bar, an announced duration, "Cancel" (building only) and, once the map is built, "Write this map to the device" (**never automatic**).

### 3.6 Interface

Single page `v650sync/index.html` (HTML + CSS + JS, no library), light / dark / automatic themes, a single scrollbar. Tabs: *Sessions* (list, detail, curves, exports), *Routes* (unified library: device + Komoot + GPX; sort by date, distance, ascent, name; filters; write, update, delete), *Map* (editor: area drawn live, routes in layers, the device's current map as an outline, Light / Bike / Detailed levels of detail, size and duration estimate), *Connections* (Intervals.icu: API key; Komoot: sign-in and session upload; automatic upload; Strava), *Device* (profile, clock, display units, data folder, log, "About" with version, link to the source code, license and the update-check setting). A small "Update available" tag appears in the top bar when a newer version is published.

**Language.** French is the source text of the page (`index.html`) and of the program's messages. In English, a browser-side translator (`tr()`) replaces the texts at display time: a dictionary of exact sentences and of patterns with variables (`{0}`, `{1}`… numbered in order of appearance), in the file `v650sync/i18n_en.json` (~450 sentences, ~160 patterns), injected into the page; an observer (`MutationObserver`) also translates dynamically added text and the `title` / `placeholder` attributes. A text without a translation stays in French, never an error. The log and the code stay in French. Choice: automatic (Windows language), `fr` or `en` (button at the top of the page, `lang` setting).

**Units.** Independent of the language: the program follows the device's preference (metric or imperial, read from its profile and stored in `device_metric` so the display is right at the next launch), or a manual choice (`units` setting: `auto` / `metric` / `imperial`). The server always returns km, km/h and m; conversion (miles, mph, feet, pounds, feet-inches) is done at display time.

### 3.7 Data and files

- Data folder: `V650SYNC_DATA` > the user's choice (`settings.json`) > `~/V650 Sync`. Nothing is created before the choice at first launch.
- Contents: `sauvegarde/<serial>/…` (copy of the device), `export/`, `komoot/` (GPX), `parcours/`, `cartes/`, `rapports/`, `config.json`.
- Application files in `%LOCALAPPDATA%\V650Sync\`: `settings.json` (theme, language, units, data folder, update-check setting and state), `running.json`, `journal.txt` (+ 3 older ones), profile of the Edge fallback.
- **Single instance** (mutex `Local\V650Sync`): a second launch brings the existing window to the front.

## 4. PFTP protocol and device formats

Protocol reconstructed by capturing the traffic between FlowSync and the device (Wireshark), then replayed and validated.

- **Banner**: a line starting with `Polar FTP server`.
- **Frame**: 16-bit little-endian header `(L << 2) | flags`, then `L` bytes (`L` ≤ 16,383).
- **GET**: `[len16][0x08 <cmd> 0x12 <len> <path>]` (`PbPFtpOperation`, GET = 0). A path ending in `/` is a **directory** (`PbPFtpDirectory`).
- **Reading a file**: content frames then an empty frame; beyond 16,383 bytes, the device only sends the next frame after the client returns **two empty frames** (`00 00 00 00`).
- **PUT (1) / REMOVE (3)**: `[len16][operation][data]`. **Each write is acknowledged by two empty frames** that must be read before the next request (otherwise what follows is appended to the file in progress). Answers: `00 00 00 00` = accepted; `00 00` + code = refused (observed: 106 "operation not permitted" for an unknown file name).
- **Multi-frame write**: only `/U/0/MAP/0/MAPDATA.MAP` accepts it. A single PUT whose payload is cut into 16,383-byte frames (flag 0 then 1 for continuations); the device returns the token `00 02 00 00` after each frame and we wait for this token before the next; final acknowledgement `00 00 00 00`. The other files (routes included) fit in **one frame**: a route must be ≤ 15,000 bytes.
- **Short queries** (`PbPFtpQuery`, `[0x8000 | length][id][parameters]`): 1 set UTC time, 2 read UTC time, 3 set local time and offset (whole hours), 4 read local time, 5 disk space, 8 battery.

Tree (`/U/0/` = user 0):

```
/DEVICE.BPB                     identity (model, versions)
/U/0/S/PHYSDATA.BPB, PREFS.BPB  physical profile, preferences
/U/0/YYYYMMDD/E/HHMMSS/         a session: ID, TSESS, PHYSDATA, then 00/ (BASE, STATS, LAPS, PROFILE, BIKE,
                                SAMPLES.GZB, ROUTE.GZB, RR.GZB)
/U/0/ROUTES/N/PROUTE.BPB        a route
/U/0/FAV/NN/TST.BPB, ID.BPB     the favourite that references the route (20 at most)
/U/0/MAP/0/MAPDATA.MAP, MAPINFO.BPB   offline map (Mapsforge) and its metadata
```

`.BPB` files are protobuf (definitions `PbTrainingSession`, `PbExerciseSamples`, `PbExerciseRouteSamples`, `PbPlannedRoute`… taken from the `cmaion/polar` project and checked on V650 files).

## 5. Routes

- **Format** (`PROUTE.BPB`): id, name, length, start point (lat/lon), altitude, **points `(x, y, t_ms, z)` in metres relative to the start**, creation and modification dates, and an external id field (12): FlowSync writes the Komoot tour number there, which lets us recognise the origin without querying Komoot.
- **From GPX to the device**: reading, Ramer-Douglas-Peucker simplification (≤ 450 points "Standard", ~850 "HD", ≤ 15,000 bytes), local projection (110,937 m per degree of latitude; longitude on the WGS84 ellipsoid at the start latitude — values **measured** on routes written by FlowSync, mean residual error < 1 m), cleaned name (30 characters, no accents), writing `ROUTES/N` + `FAV/NN`, compared read-back.
- **Library** (`routelib.py`): one route = one row; device / Komoot / GPX merge by the Komoot id then by name; "synchronised" if the tracks coincide (mean gap ≤ 12 m + 0.06 m/km, length within 3 %); "created" and "modified" dates as on Komoot, sorted by last modification.
- **Updating** a route: replaced at the same slot (delete then rewrite, like FlowSync); the old file is copied to `parcours/avant-mise-a-jour/`.
- Several writes can be chained; the device must be **turned off and on again** to show the result.
- **FlowSync erases the routes it does not know** (it deletes and rewrites all routes at each sync): do not use it in parallel.

## 6. Offline maps

### 6.1 What the device expects

A **Mapsforge version 3** file (256 tiles, Mercator, base zooms 5 [0–7], 9 [8–9], 14 [10–21]), produced by `mapsforge-map-writer 0.5.1` (current mapsforge version 5 maps are not readable). Tag tables vary from one Polar block to another: the device identifies tags by their **text**, not their number. `MAPINFO.BPB` (46 bytes): `{1: centre {lat, lon}, 2: UTC date-time, 3: flag}`; **flag 0 = area requested on the device, 1 = map installed**. The application can raise the flag itself: no more manipulation on the device.

### 6.2 How FlowSync does it (observed)

FlowSync downloads a regional block from a Polar CDN, then writes `MAPDATA.MAP` in a single multi-frame PUT and `MAPINFO.BPB` (flag 1). The trigger comes from the device (flag 0 + new centre in `MAPINFO.BPB`). The blocks follow a **grid**: 150 km cells, 1.35° in latitude, longitude step 150 km / (111.195 × cos(latitude of the north edge)) rounded to the hundredth; the block is the cell containing the requested area plus its 8 neighbours (450 km on a side), prediction verified on three blocks. These are private Polar services with no public API: the application does not rely on them.

### 6.3 Building

1. **Area**: drawn in the editor (centre of a 50 to 200 km square, rectangle, circle, polygon, pencil) or a **band around the chosen routes** (rectangle oriented per simplified segment, discs at the ends and at bends > 8°, `corridor_rings`), adjustable margin. Rings written to a `zone.poly`.
2. **OpenStreetMap extracts**: Geofabrik regions covering the area (`pick_regions`), downloaded as needed (sizes announced in the plan); the **data date** (header of the `.pbf`) is shown in the plan, the list of maps and the installed map.
3. **Osmosis 0.48.3** launched directly with Java 17 (plexus-classworlds):
   `--read-pbf … --bounding-polygon (zone.poly) [--merge] --tag-transform (addresses removed) --mapfile-writer … zoom-interval-conf=5,0,7,9,8,9,14,10,21`. Osmosis 0.49 is incompatible with the plugin; even with 0.48.3 an `AbstractMethodError: close()` error appears **after** writing (non-zero exit code with no effect): the produced file is therefore checked (`inspect_map`), not the exit code.
4. **Strict clipping** (`completeWays=no completeRelations=no`, the default): measured on a 284 km² band, "complete" gave 50 % of tiles outside the area and 230 s; strict mode 80 % inside the area and 20 s. Trade-off: at the edge, a clipped forest or lake may lose its fill. `tile_coverage` measures the real shape from the tile index.
5. **Levels of detail** Light / Bike (default) / Detailed: three tag files; the device displays shop names but not their icons.
   **Appearance zoom per tag**: each way appears from a zoom level that depends on its type. These levels are read from Polar's map (ways with **a single tag**, 20 areas, `mapdata/zoom_polar.json`, 105 tags) and applied to the tag files: main network, railways, watercourses and large forests from zoom 10; service roads, service ways, tracks, most surfaces (grass, meadows, wasteland…) from 12; paths, footpaths, steps from 13; gardens, parks and playgrounds from 15. A first version left Osmosis's default levels (tracks and service ways later, steps at 16): at low zoom, some roads were missing compared with Polar.
6. **Writing** (`mapservice.write_map`): file check, **mandatory local copy** of the current map, "write in progress" marker, flag raised, `MAPDATA.MAP`, `MAPINFO.BPB`, **full read-back and SHA-256 comparison**, resumable if interrupted.

The device displays a large **white square** around the area (bounding rectangle of the Mapsforge header): unavoidable, the area itself is exact. The generated maps are denser than Polar's (~1 MB per 100 km² against 0.16): aim for areas of 100 to 200 km on a side, or bands around routes. A warning appears above 8,000 km² or 3 regions.

The tools are pinned by SHA-256 checksum and downloaded **on demand**, from their official sites (Eclipse Temurin, GitHub, Maven Central, Geofabrik).

### 6.4 Contour lines (option)

- **What Polar's maps contain** (measured on one block): lines tagged `contour_ext=elevation_major|medium|minor`, plus `ele=<altitude>` for the first two classes. Major every 500 m and medium every 100 m (visible from zoom 10), fine about every 25 m (from zoom 11): the fine equidistance is deduced from measured lengths (ratio ≈ 15 lines for 500 m). They weigh little (~0.07 MB per 100 km²).
- **Elevations**: 1° SRTM `.hgt.gz` tiles (3601 × 3601 points, ~30 m; 16.6 MB compressed) from AWS's public "Terrain Tiles" dataset, no account (`elevation-tiles-prod/skadi`). Public domain (USGS: credit the source). The sea has no tile (the server answers 403): the area is then at 0 m, with no contour lines. Tiles are kept in `cartes/altitudes/` of the data folder.
- **Computation** (`v650sync/contours.py`, standard Python, no numpy): mosaic of the tiles, cells of the area only (the cost follows the surface, not the bounding rectangle), contour lines by "marching squares", joining of segments, simplification (Douglas-Peucker, 6 m), at most 4 million cells (beyond that: one point out of two, then three…). Measured: 960 km² at 30 m in 2.4 s, ~1,500 lines, 117,000 points.
- **Integration**: OSM XML file (very high ids, mandatory timestamp), read by `--read-xml` then merged (`--merge`) with the OpenStreetMap extract before writing. **No `--bounding-polygon` on this stream**: with the very high ids of the contours, this filter removed whole lines (4,982 ways kept out of 6,948, 18 empty tiles on the map); the computation already stops at the edges of the area (one cell of margin). The contour tags have `zoom-appear` 10 / 11 as at Polar and **`force-polygon-line`**: without it, a closed contour is taken for a surface and the device draws a grid at tile edges. Osmosis no longer exits after an XML read: the application stops it 20 s after "Finished writing file".
- **Result**: a 28 × 28 km map around Annecy, 4.26 MB with all contour lines against 3.69 MB without (+15 %; about +4 % with main lines only), built in 43 s.
- **Verified**: the contour lines display on the device (validated by the author). Not studied in detail: fineness, legibility at 100 m and 200 m, smoothness; they stay off by default.

## 7. External integrations

| Service | Role | Implementation |
|---|---|---|
| **Komoot** | Import routes; upload sessions | undocumented internal interface; `kompy` logic rewritten without the dependency (`komoot.py`): Basic sign-in then encrypted token, paginated list, GPX export; pauses between requests; e-mail masked in the log. **Upload**: `POST /v007/tours/?data_type=fit&sport=…&name=…`, body = the FIT as raw bytes; Komoot answers 201 (created) or 202 (session already known: nothing is created or changed); Komoot gives the session the **account's default visibility** (the application does not touch it, as with Polar Flow); the request is never replayed automatically |
| **Intervals.icu** | Upload sessions (FIT) | REST API: `POST /api/v1/athlete/{id}/activities` (multipart), Basic authentication `API_KEY:<key>`; **a custom `User-Agent` header is mandatory** (Cloudflare blocking refuses Python's); the athlete id falls back to "0" if refused; the upload is remembered to display "✓ Intervals" |
| **GitHub** | Update notice | `GET https://api.github.com/repos/keltiss38/v650-sync/releases/latest` (`updates.py`), anonymous, at most **once a day**, only the version number is sent (`User-Agent` header); compares the latest release tag with the program version; the release page address must start with the repository's, otherwise it is replaced by the releases page; **nothing is downloaded or installed**. Can be turned off (`update_check`); network failure or a repository without a release: silent |
| **Strava** | — | no relay or API: "Strava" downloads the FIT and opens Strava's upload page |
| **OpenStreetMap** | Background of previews and map data | `tile.openstreetmap.org` tiles (respect their usage policy); Geofabrik extracts |

**Automatic upload** (off by default): when enabled, the sessions already copied are "known" (`auto.baseline`); after each error-free read, newer sessions are sent to Intervals.icu and/or Komoot (once only, retried on failure) and/or written as GPX in `export/`. The history is never sent in bulk.

## 8. Local API

Server on `127.0.0.1`; `POST` requests require the page's token.

| Method | Path | Role |
|---|---|---|
| GET | `/`, `/favicon.png` | page (token and theme injected), icon |
| GET | `/api/job`, `/api/jobs` | "device" task; both tasks (`main`, `build`) and the server time |
| GET | `/api/status`, `/api/profile` | device status and profile |
| GET | `/api/sessions`, `/api/session/<id>`, `/download/<id>.fit\|gpx` | sessions (local copy) and exports |
| GET | `/api/routes`, `/api/library` | device routes; unified library |
| GET | `/api/komoot/tours`, `/api/services` | Komoot list; public configuration (no secret) |
| GET | `/api/map/status` | installed map status, local maps |
| GET | `/api/journal`, `/api/data-dir`, `/api/ping`, `/api/focus` | log (version, paths), data folder, heartbeat, bring to front |
| GET | `/api/update` | stored state of the update check (no network) |
| POST | `/api/connect`, `/api/sync` | connection + read (task), read only |
| POST | `/api/set-time` | **writes** the clock |
| POST | `/api/preview`, `/api/put-route`, `/api/update-route`, `/api/remove-route` | preview; **writes / updates / deletes** a route |
| POST | `/api/library/komoot-refresh`, `/api/library/komoot-gpx`, `/api/library/detail` | Komoot list, GPX, route detail |
| POST | `/api/komoot/login`, `/logout`, `/fetch` | Komoot account |
| POST | `/api/intervals/config`, `/api/disconnect`, `/api/upload`, `/api/auto` | Intervals.icu; session upload (`target` = `intervals` or `komoot`); automatic upload |
| POST | `/api/map/plan`, `/coverage`, `/build`, `/cancel`, `/install-tools`, `/write`, `/delete` | maps (plan, coverage, build, cancel, tools, **write**, local delete) |
| POST | `/api/zip`, `/api/theme`, `/api/lang`, `/api/units`, `/api/units/seen`, `/api/pick-folder`, `/api/open-folder`, `/api/open-external` | batch exports, theme, language, units (choice and preference seen on the device), folder, fixed openings |
| POST | `/api/update/check`, `/api/update/enable` | update check (automatic if due, or forced); enable / disable |
| POST | `/api/journal/report`, `/api/journal/open`, `/api/clientlog` | bug report, log folder, JavaScript errors from the page |
| POST | `/api/quit`, `/api/closing` | graceful stop |

Command line (`python -m v650sync …`): `info`, `ls`, `backup`, `export`, `routes`, `route-from-gpx`, `put-route`, `remove-route`, `set-time` (commands that write require `--yes`).

## 9. Log, tests and build

- **Log** (`journal.py`, stdlib `logging`, rotation 4 × 1 MB): header (versions, system, data folder, WebView2), page calls, tasks, device sessions, every PFTP write (path, size, duration, refusal), Komoot, Intervals.icu (HTTP status, server, response excerpt), downloads, Osmosis, state of the settings at startup ("Intervals.icu saved / Komoot connected", without value), exceptions. Detailed level with `V650SYNC_DEBUG=1`. **Masking**: e-mail addresses, `token/password/authorization/api key = value`, `Basic/Bearer …`, home folder → `~`; never coordinates or contents of device files. **Bug report**: local zip (`rapports/`) with versions, logs and the last three `osmosis.log`; nothing is sent; it may contain route names and paths.
- **Tests**: `python -m unittest discover -s tests` (70 tests: decoders, FIT, routes, fake PFTP device, fake Komoot (read and upload), Intervals and GitHub, area clipping, contour lines, map writing with a fake device, log and masking, real server, settings, consistency of the translation dictionary).
- **Build**: `pip install -r requirements-dev.txt` then `pyinstaller v650sync.spec` → `dist/V650Sync.exe`.
- **Environment variables**: `V650SYNC_DATA` (data folder), `V650SYNC_DEBUG=1` (detailed log), `V650SYNC_KOMOOT_URL` (tests: replaces the Komoot API with a local server).

## 10. Measurements and validations

- **Exported FIT vs Polar Flow FIT**: identical session totals; timestamps, positions, distance, speed, altitude, cadence 100 %; HR 99.5 %. Rules established: timestamp of record *i* = `floor((i + 1) × interval) + pauses`; position index = `round(D / interval) − 1`; altitude = sum of the calibration. GPX identical to Polar Flow's.
- **Connection**: first connection 15 s → 3.3 s thanks to the parallel attempts; full read ~36 s, then ~18 s skipping already copied sessions.
- **Routes**: track written by the application overlaid on FlowSync's on the device's screen (photos at 200 m scale), with more points in bends; the initial projection (111,195 m/° × cos) was off by ~20 m on average, corrected by measurement.
- **Maps**: maps of 7 to 107 MB written and displayed by the device; writing ~2.2 MB/s, full read-back, identical checksum; building a 15 km band around a 215 km route: 21 MB in 4 min.
- **Exe**: launch, no console, native window, close button → clean stop, second launch → existing window.

## 11. Problems met

| Problem | Cause | Fix |
|---|---|---|
| No banner / silent device | stale mDNS records; previous session not closed | try all services; clean close |
| Large files truncated | only one empty frame sent | two empty frames |
| Corrupted `PROUTE.BPB` (+67 bytes) | only one of the two acknowledgements read | read both empty frames |
| Shifted FIT | timestamp and position rules | rules established by comparison (§10) |
| Silent device after the server stopped | server killed mid-read | graceful stop |
| Progress window never displayed | error (local `import time`) in the server, masked | fix + test with a real server |
| Map filling the whole rectangle | "complete ways/relations" | strict clipping by default |
| Intervals.icu: HTTP 403 | Cloudflare blocking Python's default User-Agent | custom User-Agent |
| Intervals.icu key lost on relaunch | several copies of `config.json` read then rewritten; a failed test erased the key | single, re-read settings object, key kept if a new attempt fails, state shown in the tab |
| Time unreadable in light theme | fluorescent green text on a light background | colour adapted to the theme |
| Roads missing at low zoom (compared with Polar) | Osmosis's default appearance levels, later than Polar's | levels read from Polar's map and applied (`zoom_polar.json`) |
| Grid of straight lines on maps with contour lines | a closed contour is taken for a surface | `force-polygon-line` tag on contours |
| Contour lines missing in rectangular blocks | Osmosis's `--bounding-polygon` filter removed lines with very high ids | filter removed on the contour stream (the computation is already limited to the area) |
| Weight change has no effect on the device | `PHYSDATA.BPB` written (PUT accepted) and read back identical, but the device still shows the old weight, even after a restart | the device does not use this file as its source of truth (or rewrites it from other data): the profile-editing feature was **removed**, the owner and profile display stays read-only |
| Comparison images: missing line pieces | the map reader only drew the first piece of a line cut by tiles | all pieces are drawn (no defect in the map files) |

## 12. Known limitations and ideas

**Limitations**: a single device (no multi-device); time zone offset in whole hours; routes ≤ ~15 KB (one frame); white square around maps; border area without fill; Windows 10 untested; the English interface is a runtime translation (a few messages stay in French: native Windows dialogs, the first window to choose the folder, the log); unsigned exe (SmartScreen); Komoot's internal interface may change.

**Ideas**: decide whether to turn contour lines on by default, more city names in the editor, code signing and an installer, full FIT (RR, temperature), multi-device, a review of the English by a native speaker, other languages.

**Will not be done**: firmware modification; Strava API; training analysis; editing the profile (weight, height…): tried on the real device, no visible effect (§11).
