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


**Project by cubantonystark**
