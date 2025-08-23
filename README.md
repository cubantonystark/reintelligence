# reintelligence

A Flask web application for interactive property search, analysis, and visualization.

## Features

- **Interactive Map:**  
  View properties for sale in Tampa, FL on a live map using Folium and Leaflet. Home markers update automatically as you pan or zoom the map.

- **Property Details:**  
  Click any marker to see property address, price, number of beds/baths, and listing photo. Direct link to the Zillow listing included.

- **AI-Powered Analysis:**  
  Click the "Good deal?" button on any property marker to request an automated AI powered investment analysis, including comparable sales and financing scenarios.

## Code Overview

- **map.py**  
  - Flask backend serving the map and handling API requests.
  - Fetches property data from Zillow's RapidAPI.
  - Filters results by map bounds.
  - Handles requests for AI analysis and returns results.

- **templates/base.html**  
  - Frontend rendering the Folium map and property markers.
  - JavaScript code for dynamic marker updates and popup handling.
  - AJAX calls for map refresh and AI analysis.

## Getting Started

1. Install dependencies: Flask, folium, and OpenAI Python SDK.
2. Set up RapidAPI and OpenAI API keys in your environment.
3. Run `map.py` and visit the app in your browser.

---

# Engineering explanation (how it all works)

## High-level architecture
- **Frontend (Leaflet + Vanilla JS in `base.html`)**
  - Renders an interactive map with two base layers (Streets/Satellite).
  - Handles geolocation on first load (desktop/mobile) to center the map near the user.
  - Debounced map-move/zoom events call the backend `/refresh` to fetch properties in view.
  - A search box calls `/lookup`:
    - If input looks like a full address → center on the single property and **do not** trigger area refresh (keeps the popup open).
    - If input looks like a city/region → geocodes, recenters with a **close-in city zoom**, and then does normal area refresh.
  - Each property marker’s popup has a **“Good deal?”** button that calls `/clicked` to generate the investment report.
  - **i18n**: English/Spanish UI strings (including **layer names**), live toggle.
  - **Mobile UX**: z-index & top offsets ensure Leaflet controls and report buttons sit **above** the header.

- **Backend (Flask in `map.py`)**
  - Endpoints:
    - `GET /`: serves the page.
    - `POST /refresh`: returns properties for current map bounds (uses reverse geocoding → city/ZIP → Zillow).
    - `POST /lookup`: address vs. place flow; for a valid address, look up ZPID and fetch **single** property detail; for a place, geocode and return center/zoom.
    - `POST /clicked`: calls OpenAI to build a structured HTML report + **Grade A–F**, using nearby comps and any available **Features/HOA/CDD**.
    - `GET /debug/ping`: health check.
  - **External services**:
    - **Zillow RapidAPI** for search/extended search/property details.
    - **OpenStreetMap Nominatim** for forward & reverse geocoding.
    - **OpenAI** for the formatted investment report.

## Data flow
1. **Initial load**: Browser tries `navigator.geolocation`; map is created and `setupMapRefresh()` triggers `/refresh`.
2. **/refresh**:
   - Receives map bounds and center/zoom.
   - Debounces repeated/near-identical requests (time & small-move thresholds).
   - Reverse geocodes center → “City, State” or ZIP.
   - Calls `fetch_homes(location, bounds)` which requests **Zillow extended search** (with caching). Returns normalized listing cards (lat/lon/address/price/beds/baths/img/detail URL/last sold).
3. **/lookup**:
   - Heuristics (`looks_like_address`) split address flow from place flow.
   - Address:
     - Cache-first **ZPID** lookup via `/search` and fallback fuzzy match via `/propertyExtendedSearch`.
     - Then `/property?zpid=…` for detailed info (lat/lon/address/price/beds/baths/images/detail URL + **features**, **HOA/CDD** best-effort).
     - Responds with `mode: "property"` and a single home + center.
   - Place (city/state/etc.):
     - Forward geocodes and returns `mode: "place"` with center + a **close-in zoom** (city ~16).
4. **Marker popup’s “Good deal?” → /clicked**:
   - Server collects the subject info (plus features/HOA/CDD if present).
   - Builds nearby comps by progressively expanding radius until ≥5 comps (cap 12).
   - **OpenAI** prompt:
     - **System message** locks language (EN or ES) so the **entire report** is in the selected language.
     - Model must return the **first line** as `GRADE: X`, then **pure HTML only** (no `<html>`/`<head>`).
   - Server **sanitizes** any code fences, extracts the grade, wraps HTML with a clean, responsive shell (Material Icons, fine-print rubric and disclaimer).
5. **Frontend** shows the print-friendly modal report (with correct z-index on mobile) and language persists.

## Reliability & performance concerns handled
- **Caching (in-memory)**:
  - `props_by_location` (extended search payloads).
  - `zpid_by_query` (normalized query → zpid).
  - `property_by_zpid` (property details).
  - **TTL** controls per bucket are configurable via env vars.
- **Backoff & pacing**:
  - A **minimum interval** between RapidAPI calls.
  - If a `429` is returned, a **cooldown window** is set to avoid hammering the API.
- **Debounce**:
  - `/refresh` requests skip if map center/zoom changed only slightly and within a short interval.
- **Normalization/heuristics**:
  - Address normalization helps match street suffix variants.
  - Fallback fuzzy matching against extended-search results if `/search?query=` doesn’t produce ZPID.
  - Defensive parsing for **non-uniform Zillow payload shapes** (lat/lon/price/features across multiple keys).
- **Security/robustness**:
  - API keys via environment variables.
  - Strict JSON handling and escaping injected HTML report content (we wrap & sanitize model fences).
  - Timeouts on external HTTP calls.

## UX details that matter
- **Property focus mode**: when a validated address is displayed, **no area refresh** runs until popup closes—keeps the balloon open.
- **City vs. Address zooms**: city ~**16**, address **17**.
- **Layers**: localized names; toggling language rebuilds the layer control so labels update.
- **Mobile controls**: Leaflet zoom/layers and report overlay **stay visible** above the fixed header.

## Key functions & responsibilities
- `fetch_homes(location, bounds)`: cache-first Zillow extended search; normalize listing objects; optional bounds filter.
- `zillow_search_get_zpid(query)` + `zillow_fuzzy_from_extended(query)`: robust ZPID discovery.
- `zillow_property_details_by_zpid(zpid)`: unify details and mine **features**, **HOA/CDD** if present.
- `geocode_place(q)` and `reverse_geocode(lat,lon)`: Nominatim helpers for place resolution and UI-friendly location names.
- `extract_grade_and_html()`: parses OpenAI output (`GRADE: X` then HTML).
- `wrap_report_html()`: professional shell with **grade chip** in the header.

## Configuration knobs (env vars)
- `OPENAI_API_KEY`, `RAPIDAPI_KEY` (required for external calls).
- Cache TTLs: `TTL_PROPS_BY_LOCATION`, `TTL_ZPID_BY_QUERY`, `TTL_PROP_BY_ZPID`.
- Call pacing: `ZILLOW_MIN_INTERVAL`, `ZILLOW_429_BACKOFF`.
- Debounce sensitivity: `MIN_REFRESH_INTERVAL`, `MIN_CENTER_DELTA_DEG`.

---

# TL;DR (for engineers)

- **Flask API** + **Leaflet UI** that:
  - Finds properties via **Zillow RapidAPI**, caches responses, rate-limits calls, and debounces map refreshes.
  - **Lookup**: address → ZPID → single property; place → geocode → close-in city zoom.
  - **Report**: OpenAI generates a graded (A–F) HTML investment report using subject data, **features**, **HOA/CDD**, and ≥5 **comps**; server sanitizes and wraps it.
  - **i18n**: Live EN/ES switching (UI, layers; report forced via system message).
  - **Mobile**: Leaflet controls & report actions always visible above the fixed header.
# Changelog — Map & Report App

## TL;DR
- Added **/lookup**, **/refresh**, **/clicked** endpoints and robust Zillow/Nominatim integrations with **caching**, **rate limiting**, and **429 backoff**.  
- Implemented **address vs. place** search logic, **close-in zoom** (city ~16, address 17), **property-focus** mode (no refresh while popup open).  
- Added **geolocation on first load**, **Spanish/English** UI + **report localization**, and **mobile-safe** Leaflet controls & report modal.  
- Report now shows **Investment Grade (A–F)** in header, includes **features**, **HOA/CDD**, and **≥5 comps** with expanded radius.  
- Introduced **structured logging** (console + rotating file).  
- Multiple UI/UX fixes: localized layer labels, spinner shown **only** for the “Good deal?” flow, better address rendering, sanitized model HTML.

---

## Backend (`map.py`)

### New endpoints & flows
- **`POST /lookup`**:  
  - Detects **address vs. place** with heuristics.  
  - **Address** → `/search?query=` to get **zpid**, fallback to fuzzy match via `/propertyExtendedSearch`, then `/property?zpid=` to get details; returns `{ mode: "property", center, home }`.  
  - **Place** (city/state/ZIP) → forward geocode via **Nominatim** and returns `{ mode: "place", center, zoom }`.
- **`POST /refresh`**:  
  - Reverse geocodes map center to a friendly location (ZIP or `City, State`).  
  - Fetches homes via **Zillow `propertyExtendedSearch`** for current bounds.  
  - Includes **debounce** guard to avoid redundant queries (time & small-move threshold).
- **`POST /clicked`**:  
  - Builds **investment report** with OpenAI (first line `GRADE: X`, then HTML only).  
  - Ensures **≥5 comps** by progressively expanding radius; injects **features** and **HOA/CDD** when available.  
  - **Language** is enforced via an OpenAI **system message** (EN/ES).
- **`GET /debug/ping`**: health check with UTC timestamp.

### Zillow & geocoding integration
- Added `_zillow_http_get()` with:
  - **Minimum interval** between calls (`ZILLOW_MIN_INTERVAL`).  
  - **429** handling with a **cooldown backoff** (`ZILLOW_429_BACKOFF`).  
  - Response logging (status/bytes/preview).  
- Introduced **caching** buckets:
  - `props_by_location` (extended search payloads).  
  - `zpid_by_query` (normalized query → zpid/address).  
  - `property_by_zpid` (property details).  
- Added **address normalization** + fuzzy matching to improve ZPID discovery.  
- Added **Nominatim** helpers: `geocode_place()` (returns lat/lon/zoom tuned per place type), `reverse_geocode()` (produces ZIP or `City, State` strings).

### Report generation
- **Language enforcement** (ES/EN) via OpenAI **system** role.  
- Report HTML is **sanitized**: code fences removed, then wrapped in a **responsive** shell.  
- **Grade chip** moved to the report **header**; “Investment Analysis” label removed.  
- Injects **subject features**, **HOA/CDD** (best-effort from Zillow shapes).  
- Comp selection: **expand radius** until ≥5 comps (cap 12).  
- Wrapper injects a **bilingual rubric + disclaimer** (fine print).

### Reliability & robustness
- **Debounce** for `/refresh` using last center/zoom + time guard.  
- **Structured logging**:
  - `logging` with **StreamHandler** (console) + **RotatingFileHandler** to `logs/app.log`.  
  - Consistent tags: `[ZILLOW][REQ]`, `[ZILLOW][RES]`, `[REFRESH]`, `[LOOKUP]`, `[OPENAI]`, etc.  
- **Env-driven config**: API keys, cache TTLs, rate limits, debounce thresholds.  
- Address rendering fixed: **`address_to_string()`** to avoid `[object Object]` in popups.  
- Defensive parsing for varied Zillow payload shapes (lat/lon/price/images/features, etc.).

---

## Frontend (`templates/base.html`)

### Search & map behavior
- **Search box** submits to `/lookup`:
  - **Address** → centers on property at **zoom 17**, shows **only that marker**, opens popup, and **suppresses area refresh** until popup closes.  
  - **Place (city/state/ZIP)** → centers with a **close-in zoom ~16** and **runs normal refresh**.
- **Debounced refresh** (`moveend`/`zoomend`) calls `/refresh` after a short delay.  
- **Geolocation** on first load (desktop & mobile):
  - Attempts `navigator.geolocation` to center map near the user; **Tampa** fallback.

### UI/UX & mobile fixes
- **Leaflet controls** (zoom & layers) now **stay below the fixed header** on phones:  
  - `.leaflet-control-container { z-index: 10000 }`  
  - Mobile breakpoint shifts `.leaflet-top { top: 66px !important }`.  
- **Report modal** (print/close) z-index and sticky header ensure buttons are **always visible** on mobile.  
- **Spinner** limited to **“Good deal?”** flow only (removed during property lookups).

### Internationalization (i18n)
- **Live EN/ES toggle** updates:
  - Input placeholder, Search text, spinner label, popup action text, **layer names** (“Streets/Satellite” ↔ “Calles/Satélite”).  
- **Report language** is **fully aligned** with UI toggle via backend system message.  
- Layer control labels rebuild when language changes to update text.

### Marker & popup rendering
- Unified popup template with localized labels and link to Zillow detail URL.  
- **Keep-open behavior**: while focused on a single property, area refresh is disabled; popup close **re-enables** refresh.

---

## Configuration (new/used environment variables)
- `OPENAI_API_KEY` — OpenAI access.  
- `RAPIDAPI_KEY` — Zillow RapidAPI key.  
- `TTL_PROPS_BY_LOCATION` — cache seconds for extended search payloads (default **600**).  
- `TTL_ZPID_BY_QUERY` — cache seconds for zpid lookup (default **3600**).  
- `TTL_PROP_BY_ZPID` — cache seconds for property details (default **3600**).  
- `ZILLOW_MIN_INTERVAL` — minimum seconds between Zillow calls (default **1.5**).  
- `ZILLOW_429_BACKOFF` — cooldown seconds after 429 (default **8.0**).  
- `MIN_REFRESH_INTERVAL` — debounce window for `/refresh` (default **4.0**).  
- `MIN_CENTER_DELTA_DEG` — minimal delta in degrees to treat as a new center (default **0.01**).

---

## Potentially breaking changes
- **Layer control** is rebuilt on language toggle; custom third-party overlays would need to be re-registered after rebuild if added later.  
- **Report HTML contract**: OpenAI response must start with `GRADE: X`. Server strips any code fences and wraps content, so any previous consumer expecting raw Markdown/HTML fences will differ.  
- **Refresh behavior** is now **debounced** and **skips tiny moves**, which changes the frequency of calls to `/refresh`.

---

## Suggested commit bullets
- feat(api): add `/lookup`, `/refresh`, `/clicked`, `/debug/ping` endpoints  
- feat(search): address vs place detection; address→ZPID→property; place→geocode + close-in zoom  
- feat(map): initial geolocation (desktop/mobile), close-in city zoom (16), address zoom (17)  
- feat(cache): add in-memory caches for props-by-location, zpid-by-query, property-by-zpid with TTLs  
- feat(rate): add Zillow min-interval pacing + 429 cooldown backoff  
- feat(report): OpenAI-generated HTML report with `GRADE: X` header chip; includes features + HOA/CDD; ≥5 comps radius expansion; EN/ES via system role  
- fix(ui/mobile): keep Leaflet zoom/layer controls below fixed header; ensure report modal buttons stay visible  
- fix(lookup): prevent area refresh while focused on a single property; resume after popup close  
- fix(address): render human-readable addresses (`address_to_string`) instead of `[object Object]`  
- chore(logging): add structured console + rotating file logs under `logs/app.log`  
- chore(i18n): localize layer labels; hook language toggle to rebuild layer control  
- chore(style): sanitize OpenAI output; responsive report wrapper with Material Icons; spinner only for “Good deal?” flow


**Project by cubantonystark**
