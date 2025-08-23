import http.client
import json
import logging
import os
import pathlib
import re
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from threading import Lock
from urllib.parse import quote

import requests
from flask import Flask, jsonify, render_template, request

# ---------- OpenAI ----------
from openai import OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "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")
oi_client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__, template_folder="templates")

# ---------- Logging ----------
LOG_DIR = pathlib.Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("property_app")
logger.setLevel(logging.INFO)
_formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
fh = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=5_000_000, backupCount=3)
fh.setFormatter(_formatter)
sh = logging.StreamHandler(); sh.setFormatter(_formatter)
if not logger.handlers:
    logger.addHandler(fh); logger.addHandler(sh)
def log(msg: str): logger.info(msg)

# ---------- Config ----------
RAPIDAPI_HOST = "zillow-com1.p.rapidapi.com"
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "a21f37a14emsh4a6f745b07a0863p130fc3jsnb0ac087aeaa9")

TTL_PROPS_BY_LOCATION = int(os.getenv("TTL_PROPS_BY_LOCATION", "600"))
TTL_ZPID_BY_QUERY     = int(os.getenv("TTL_ZPID_BY_QUERY", "3600"))
TTL_PROP_BY_ZPID      = int(os.getenv("TTL_PROP_BY_ZPID", "3600"))

ZILLOW_MIN_INTERVAL   = float(os.getenv("ZILLOW_MIN_INTERVAL", "1.5"))
ZILLOW_429_BACKOFF    = float(os.getenv("ZILLOW_429_BACKOFF", "8.0"))

# ---------- Cache & Rate limiting ----------
_cache_lock = Lock()
_cache = {
    "props_by_location": {},
    "zpid_by_query": {},
    "property_by_zpid": {},
}
_last_request_monotonic = 0.0
_cooldown_until = 0.0
def _now(): return time.time()
def _cache_get(bucket: str, key: str, ttl: int):
    with _cache_lock:
        entry = _cache[bucket].get(key)
        if not entry: log(f"[CACHE][MISS] {bucket}:{key}"); return None
        if _now() - entry["ts"] > ttl:
            log(f"[CACHE][EXPIRED] {bucket}:{key}")
            _cache[bucket].pop(key, None); return None
        log(f"[CACHE][HIT] {bucket}:{key}"); return entry
def _cache_set(bucket: str, key: str, value: dict):
    with _cache_lock:
        _cache[bucket][key] = {"ts": _now(), **value}
        log(f"[CACHE][SET] {bucket}:{key}")
def _rate_limit_wait():
    global _last_request_monotonic, _cooldown_until
    now = time.monotonic()
    if now < _cooldown_until:
        sleep_for = _cooldown_until - now
        log(f"[RATE] Backoff active, sleeping {sleep_for:.2f}s")
        time.sleep(sleep_for); now = time.monotonic()
    elapsed = now - _last_request_monotonic
    if elapsed < ZILLOW_MIN_INTERVAL:
        sleep_for = ZILLOW_MIN_INTERVAL - elapsed
        log(f"[RATE] Spacing calls: sleeping {sleep_for:.2f}s")
        time.sleep(sleep_for)
    _last_request_monotonic = time.monotonic()
def _trigger_backoff():
    global _cooldown_until
    _cooldown_until = max(_cooldown_until, time.monotonic() + ZILLOW_429_BACKOFF)
    log(f"[RATE] 429 detected. Cooldown until { _cooldown_until :.2f} (monotonic)")

# ---------- Helpers ----------
def reverse_geocode(lat, lon):
    url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lon}"
    headers = {'User-Agent': 'PropertyMapApp/1.0'}
    try:
        resp = requests.get(url, headers=headers, timeout=6)
        data = resp.json()
        address_obj = data.get('address', {})
        city = (address_obj.get('city') or address_obj.get('town') or
                address_obj.get('village') or address_obj.get('county'))
        state = address_obj.get('state')
        postcode = address_obj.get('postcode')
        if postcode: return postcode
        if city and state: return f"{city}, {state}"
        if state: return state
        return "Tampa, FL"
    except Exception as e:
        log(f"[GEOCODE] reverse_geocode failed: {e}")
        return "Tampa, FL"

def address_to_string(addr):
    if not addr: return ""
    if isinstance(addr, str): return addr.strip()
    if isinstance(addr, dict):
        parts = []
        for k in ("streetAddress", "street", "line", "addressLine", "addressLine1"):
            if addr.get(k):
                parts.append(str(addr[k]).strip()); break
        city = addr.get("city") or addr.get("locality")
        state = addr.get("state") or addr.get("region")
        zipc = addr.get("zipcode") or addr.get("postalCode") or addr.get("zip")
        tail = ", ".join([p for p in [city, state] if p])
        if tail: parts.append(tail)
        if zipc: parts.append(str(zipc))
        s = " ".join(parts).strip().replace("  ", " ")
        return s if s else str(addr)
    return str(addr)

def _zillow_http_get(path: str):
    _rate_limit_wait()
    conn = http.client.HTTPSConnection(RAPIDAPI_HOST, timeout=15)
    headers = {
        'x-rapidapi-key': RAPIDAPI_KEY,
        'x-rapidapi-host': RAPIDAPI_HOST,
        'Accept': 'application/json',
        'User-Agent': 'PropertyMapApp/1.0'
    }
    log(f"[ZILLOW][REQ] GET {path}")
    try:
        conn.request("GET", path, headers=headers)
        res = conn.getresponse()
        status = res.status
        raw = res.read()
        body_preview = raw[:400].decode("utf-8", errors="ignore")
        log(f"[ZILLOW][RES] status={status} bytes={len(raw)} preview={body_preview!r}")
        if status == 429: _trigger_backoff()
        if status != 200: return status, None
        try:
            data = json.loads(raw.decode("utf-8", errors="ignore"))
            return status, data
        except Exception as je:
            log(f"[ZILLOW][JSON] decode error: {je}")
            return status, None
    except Exception as e:
        log(f"[ZILLOW][ERR] {e}")
        return 0, None

# ---------- Zillow helpers ----------
def get_zip_or_city(address):
    m = re.search(r'\b\d{5}\b', address)
    if m: return m.group(0)
    parts = [p.strip() for p in address.split(',')]
    return parts[1] if len(parts) > 1 else "Tampa"

STREET_SUFFIXES = r"(ave|avenue|blvd|boulevard|cir|circle|ct|court|dr|drive|hwy|highway|ln|lane|pkway|pkwy|parkway|pl|place|rd|road|st|street|ter|terrace|trl|trail|way)"
ADDRESS_RE = re.compile(rf"\b\d+\s+.+\b{STREET_SUFFIXES}\b", re.IGNORECASE)
def looks_like_address(q: str) -> bool:
    q = q.strip()
    return bool(ADDRESS_RE.search(q) or (re.search(r"\b\d{1,6}\b", q) and re.search(r"\b\d{5}(-\d{4})?\b", q)))

def normalize_address_simple(s: str) -> str:
    s = s.lower()
    s = re.sub(r'[\s,.-]+', ' ', s)
    s = re.sub(r'\bavenue\b', 'ave', s)
    s = re.sub(r'\bboulevard\b', 'blvd', s)
    s = re.sub(r'\bstreet\b', 'st', s)
    s = re.sub(r'\broad\b', 'rd', s)
    s = re.sub(r'\bdrive\b', 'dr', s)
    s = re.sub(r'\bterrace\b', 'ter', s)
    s = re.sub(r'\bcircle\b', 'cir', s)
    s = re.sub(r'\bparkway\b', 'pkwy', s)
    s = re.sub(r'\blane\b', 'ln', s)
    s = re.sub(r'\bhighway\b', 'hwy', s)
    return s.strip()

def split_address_citystatezip(q: str):
    parts = [p.strip() for p in q.split(",")]
    if len(parts) >= 2: return parts[0], ", ".join(parts[1:])
    return q, ""

def _cached_zpid_for_query(query: str):
    key = normalize_address_simple(query)
    entry = _cache_get("zpid_by_query", key, TTL_ZPID_BY_QUERY)
    if entry: return entry.get("zpid"), entry.get("address")
    return None, None
def _store_zpid_for_query(query: str, zpid: str, address: str):
    key = normalize_address_simple(query)
    _cache_set("zpid_by_query", key, {"zpid": zpid, "address": address})

def _cached_property_by_zpid(zpid: str):
    entry = _cache_get("property_by_zpid", zpid, TTL_PROP_BY_ZPID)
    if entry: return entry.get("payload")
    return None
def _store_property_by_zpid(zpid: str, payload: dict):
    _cache_set("property_by_zpid", zpid, {"payload": payload})

def zillow_search_get_zpid(query: str):
    zpid, addr = _cached_zpid_for_query(query)
    if zpid: log(f"[ZPID][CACHE] {query} -> {zpid}"); return zpid, addr
    status, payload = _zillow_http_get(f"/search?query={quote(query)}")
    if status != 200 or not isinstance(payload, dict):
        log(f"[ZPID][SEARCH] status={status}, no payload"); return None, None
    for key in ("results", "data", "props", "items", "suggestions"):
        items = payload.get(key)
        if isinstance(items, list):
            for it in items:
                zpid_val = (it.get("zpid") or it.get("meta", {}).get("zpid") or it.get("property", {}).get("zpid"))
                address_val = (it.get("address") or it.get("display", {}).get("address") or it.get("property", {}).get("address"))
                if zpid_val:
                    zpid_str = str(zpid_val)
                    _store_zpid_for_query(query, zpid_str, address_to_string(address_val or ""))
                    log(f"[ZPID] /search match key={key} zpid={zpid_str}")
                    return zpid_str, address_val
    log("[ZPID] /search did not return a zpid"); return None, None

def zillow_fuzzy_from_extended(query: str):
    zpid, addr = _cached_zpid_for_query(query)
    if zpid: log(f"[ZPID][CACHE] {query} -> {zpid}"); return zpid, addr
    _street, citystatezip = split_address_citystatezip(query)
    location = citystatezip if citystatezip else get_zip_or_city(query)
    status, payload = _zillow_http_get(
        f"/propertyExtendedSearch?location={quote(location)}&status_type=ForSale&home_type=Houses&limit=200"
    )
    if status != 200 or not isinstance(payload, dict):
        log(f"[ZPID][FUZZY] extended search status={status}"); return None, None
    props = payload.get("props") or []
    norm_q = normalize_address_simple(query)
    for p in props:
        addr = p.get("address") or ""
        if not addr: continue
        addr_str = address_to_string(addr)
        if normalize_address_simple(addr_str) in norm_q or norm_q in normalize_address_simple(addr_str):
            zpid_val = p.get("zpid") or (p.get("property", {}) or {}).get("zpid")
            if zpid_val:
                zpid_str = str(zpid_val)
                _store_zpid_for_query(query, zpid_str, addr_str)
                log(f"[ZPID] fuzzy matched '{addr_str}' -> zpid={zpid_str}")
                return zpid_str, addr
    return None, None

def _extract_first(payload, *keys):
    for k in keys:
        v = payload.get(k)
        if v not in (None, ""):
            return v
    return None

def zillow_property_details_by_zpid(zpid: str):
    cached_payload = _cached_property_by_zpid(zpid)
    if cached_payload: log(f"[PROP][CACHE] zpid={zpid}"); payload = cached_payload
    else:
        status, payload = _zillow_http_get(f"/property?zpid={quote(zpid)}")
        if status != 200 or not payload: return None
        _store_property_by_zpid(zpid, payload)

    def pick(*keys): return _extract_first(payload, *keys)

    lat = (pick("latitude") or pick("lat") or payload.get("property", {}).get("latitude"))
    lon = (pick("longitude") or pick("lng") or payload.get("property", {}).get("longitude"))
    address = (pick("address") or payload.get("property", {}).get("address") or payload.get("fullAddress"))
    address = address_to_string(address)
    price = (pick("price","priceRaw","unformattedPrice") or payload.get("homeInfo", {}).get("price"))
    bedrooms = (pick("bedrooms","beds") or payload.get("homeInfo", {}).get("bedrooms"))
    bathrooms = (pick("bathrooms","baths") or payload.get("homeInfo", {}).get("bathrooms"))
    img_url = (pick("imgSrc","image") or (payload.get("images", [{}])[0].get("url")
              if isinstance(payload.get("images"), list) and payload.get("images") else None))
    detail_url = (pick("detailUrl","url","hdpUrl") or payload.get("property", {}).get("url"))
    last_sold_amount = (pick("lastSoldPrice","last_sold_price","priceHistoryLastSold") or "N/A")

    # Features (best-effort)
    features = []
    for key in ("resoFacts","atAGlanceFacts","homeFacts","features","home_features"):
        val = payload.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    txt = item.get("factLabel") or item.get("label") or item.get("name")
                    val2 = item.get("factValue") or item.get("value")
                    if txt and val2:
                        features.append(f"{txt}: {val2}")
                elif isinstance(item, str):
                    features.append(item)
        elif isinstance(val, dict):
            for k, v in val.items():
                features.append(f"{k}: {v}")

    # HOA / CDD (best-effort)
    hoa = (pick("hoaFee","hoa","monthlyHoaFee","associationFee","hoaMonthlyFee") or "")
    hoa_freq = (pick("hoaFeeFrequency","associationFeeFrequency","hoaFrequency") or "")
    cdd = (pick("cddFee","cdd") or "")

    try:
        lat = float(lat) if lat is not None else None
        lon = float(lon) if lon is not None else None
    except Exception:
        lat, lon = None, None

    if lat is None or lon is None or not address:
        log(f"[PROP] details missing lat/lon/address for zpid={zpid}")
        return None

    if isinstance(last_sold_amount, (int, float)) or (isinstance(last_sold_amount, str) and last_sold_amount.replace(",", "").replace("$", "").isdigit()):
        try:
            last_sold_amount = f"${int(float(str(last_sold_amount).replace('$', '').replace(',', ''))):,}"
        except Exception:
            pass

    if isinstance(price, str):
        price_numeric = re.sub(r"[^\d.]", "", price)
        try:
            price = int(float(price_numeric))
        except Exception:
            pass

    return {
        'lat': lat, 'lon': lon, 'address': address,
        'price': price if price is not None else 'No price',
        'bedrooms': bedrooms if bedrooms is not None else 'N/A',
        'bathrooms': bathrooms if bathrooms is not None else 'N/A',
        'img_url': img_url or '', 'detail_url': detail_url or '',
        'last_sold_amount': last_sold_amount if last_sold_amount else 'N/A',
        'features': features[:30],
        'hoa': str(hoa) if hoa else "",
        'hoa_freq': str(hoa_freq) if hoa_freq else "",
        'cdd': str(cdd) if cdd else ""
    }

def fetch_homes(location, sw_lat=None, sw_lng=None, ne_lat=None, ne_lng=None):
    loc_key = (location or "").strip().lower()
    payload = None
    cached = _cache_get("props_by_location", loc_key, TTL_PROPS_BY_LOCATION)
    if cached:
        payload = cached.get("payload"); log(f"[FETCH_HOMES] using cached payload for '{location}'")
    else:
        location_encoded = quote(location)
        path = f"/propertyExtendedSearch?location={location_encoded}&status_type=ForSale&home_type=Houses&limit=150"
        status, payload = _zillow_http_get(path)
        if status == 200 and isinstance(payload, dict):
            _cache_set("props_by_location", loc_key, {"payload": payload})
        else:
            log(f"[FETCH_HOMES] FAIL location={location} status={status}")
            return []

    props = (payload or {}).get("props") or []
    log(f"[FETCH_HOMES] raw_props={len(props)} for location={location}")

    listings = []
    for home in props:
        lat = home.get('latitude'); lon = home.get('longitude')
        if lat is None or lon is None: continue
        if None not in (sw_lat, sw_lng, ne_lat, ne_lng):
            if not (sw_lat <= lat <= ne_lat and sw_lng <= lon <= ne_lng): continue

        raw_addr = home.get('address', 'No address')
        address = address_to_string(raw_addr) or 'No address'
        price = home.get('price', 'No price')
        bedrooms = home.get('bedrooms', 'N/A')
        bathrooms = home.get('bathrooms', 'N/A')
        img_url = home.get('imgSrc'); detail_url = home.get('detailUrl')

        last_sold_amount = (
            home.get('lastSoldPrice') or home.get('last_sold_price') or home.get('recentSoldPrice') or (
                home.get('priceHistory', [{}])[-1].get('price')
                if home.get('priceHistory') and isinstance(home.get('priceHistory'), list) and len(home.get('priceHistory')) > 0
                else None
            )
        )
        if last_sold_amount not in (None, 'null'):
            try:
                last_sold_amount = f"${int(float(str(last_sold_amount).replace('$', '').replace(',', ''))):,}"
            except Exception:
                last_sold_amount = str(last_sold_amount)
        else:
            last_sold_amount = "N/A"

        listings.append({
            'lat': lat, 'lon': lon, 'address': address, 'price': price,
            'bedrooms': bedrooms, 'bathrooms': bathrooms,
            'img_url': img_url or '', 'detail_url': detail_url or '',
            'last_sold_amount': last_sold_amount
        })

    log(f"[FETCH_HOMES] filtered_props={len(listings)} (after bounds)")
    return listings

def geocode_place(q: str):
    """
    Close-in zoom for places:
      - address view uses 17 (client-side)
      - city-like targets use 16
    """
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": q, "format": "jsonv2", "limit": 1}
    headers = {'User-Agent': 'PropertyMapApp/1.0'}
    resp = requests.get(url, params=params, headers=headers, timeout=7)
    resp.raise_for_status()
    arr = resp.json()
    if not arr: return None
    item = arr[0]
    lat = float(item["lat"]); lon = float(item["lon"])
    place_type = item.get("type") or ""
    if place_type in ("city","town","village","suburb","municipality","hamlet"):
        zoom = 16
    elif place_type in ("county",):
        zoom = 13
    elif place_type in ("state","administrative","region","province"):
        zoom = 9
    else:
        zoom = 15
    log(f"[GEOCODE] '{q}' -> ({lat},{lon}) type={place_type} zoom={zoom}")
    return (lat, lon, zoom)

# ---------- Refresh debounce ----------
_last_refresh = {"lat": None, "lng": None, "zoom": None, "ts": 0.0}
MIN_REFRESH_INTERVAL = float(os.getenv("MIN_REFRESH_INTERVAL", "4.0"))
MIN_CENTER_DELTA_DEG = float(os.getenv("MIN_CENTER_DELTA_DEG", "0.01"))

# ---------- Routes ----------
@app.route("/")
def index(): return render_template("base.html")

@app.route("/refresh", methods=["POST"])
def refresh():
    bounds = request.get_json() or {}
    sw_lat = bounds.get("sw_lat"); sw_lng = bounds.get("sw_lng")
    ne_lat = bounds.get("ne_lat"); ne_lng = bounds.get("ne_lng")
    center_lat = bounds.get("center_lat"); center_lng = bounds.get("center_lng")
    zoom = bounds.get("zoom") or 0
    log(f"[REFRESH] bounds SW({sw_lat},{sw_lng}) NE({ne_lat},{ne_lng}) center=({center_lat},{center_lng}) zoom={zoom}")

    prev = _last_refresh; now_ts = time.time()
    if (prev["lat"] is not None and
        abs(center_lat - prev["lat"]) < MIN_CENTER_DELTA_DEG and
        abs(center_lng - prev["lng"]) < MIN_CENTER_DELTA_DEG and
        zoom == (prev["zoom"] or 0) and
        now_ts - prev["ts"] < MIN_REFRESH_INTERVAL):
        log("[REFRESH] skipped (debounce + small move)")
        return jsonify([])

    _last_refresh.update({"lat": center_lat, "lng": center_lng, "zoom": zoom, "ts": now_ts})
    location = reverse_geocode(center_lat, center_lng)
    log(f"[REFRESH] chosen location='{location}'")

    homes = fetch_homes(location, sw_lat, sw_lng, ne_lat, ne_lng)
    if not homes:
        state = location.split(",")[-1].strip() if "," in location else "Florida"
        if len(state) < 2: state = "Florida"
        log(f"[REFRESH] empty for '{location}', retrying with state='{state}'")
        homes = fetch_homes(state, sw_lat, sw_lng, ne_lat, ne_lng)

    log(f"[REFRESH] returning {len(homes)} homes")
    return jsonify(homes)

@app.route("/lookup", methods=["POST"])
def lookup():
    data = request.get_json() or {}
    q = (data.get("q") or "").strip()
    log(f"[LOOKUP] q='{q}'")
    if not q:
        return jsonify({"mode": "error", "message": "Please enter an address, city, or state."}), 400
    try:
        if looks_like_address(q):
            log("[LOOKUP] ADDRESS: /search → zpid (with cache)")
            zpid, _ = zillow_search_get_zpid(q)
            if not zpid:
                log("[LOOKUP] /search failed; trying fuzzy from extended")
                zpid, _ = zillow_fuzzy_from_extended(q)
            if not zpid:
                return jsonify({"mode":"error","message":"Could not validate that property (try including city/state & ZIP)."}), 404
            home = zillow_property_details_by_zpid(zpid)
            if not home:
                return jsonify({"mode":"error","message":"Property found but details unavailable right now."}), 502
            log(f"[LOOKUP] SUCCESS zpid={zpid} -> center=({home['lat']},{home['lon']})")
            return jsonify({"mode":"property","center":{"lat":home["lat"],"lng":home["lon"]},"home":home})
        else:
            log("[LOOKUP] PLACE: geocoding only")
            geo = geocode_place(q)
            if not geo: return jsonify({"mode":"error","message":"We couldn't find that location."}), 404
            lat, lon, zoom = geo
            return jsonify({"mode":"place","center":{"lat":lat,"lng":lon},"zoom":zoom})
    except requests.HTTPError as e:
        log(f"[LOOKUP][HTTPError] {e}")
        return jsonify({"mode": "error", "message": f"Geocoding error: {str(e)}"}), 502
    except Exception as e:
        log(f"[LOOKUP][Unhandled] {e}")
        return jsonify({"mode": "error", "message": f"Lookup failed: {str(e)}"}), 500

# ---------- Report wrapper + sanitizer ----------
def report_wrapper_css():
    return """
<style>
  :root { --pri:#0078D4; --ink:#1f2937; --muted:#6b7280; --ok:#10b981; --warn:#f59e0b; --bad:#ef4444; --bg:#f8f8fb; }
  *{box-sizing:border-box}
  body{margin:0;padding:0;background:#fff;font-family:Inter,system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--ink)}
  .wrap{padding:14px 12px;max-width:840px;margin:0 auto}
  .hdr{display:flex;gap:10px;align-items:center;margin-bottom:12px}
  .hdr .icon{font-size:28px;color:var(--pri)}
  .title{font-size:1.05rem;font-weight:700;line-height:1.3}
  .chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;background:#e8f3ff;color:#0b63a6;font-weight:700;font-size:.85rem;margin-left:auto}
  .grid{display:grid;grid-template-columns:1fr;gap:10px;margin:12px 0}
  @media (min-width:560px){ .grid{grid-template-columns:repeat(2,1fr)} }
  @media (min-width:840px){ .grid{grid-template-columns:repeat(3,1fr)} }
  .card{background:#fff;border-radius:14px;box-shadow:0 1px 8px rgba(0,0,0,.06);padding:12px}
  .card h3{margin:0 0 8px 0;font-size:.95rem;display:flex;align-items:center;gap:6px}
  .card .material-icons{font-size:20px;vertical-align:middle;color:var(--pri)}
  .kv{display:flex;justify-content:space-between;margin:6px 0;font-size:.93rem}
  .kv .k{color:var(--muted)}
  .pill{font-weight:700}
  .ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
  .table{width:100%;border-collapse:collapse;font-size:.90rem}
  .table th,.table td{border-bottom:1px solid #eee;padding:7px 6px;text-align:right}
  .table th:first-child,.table td:first-child{text-align:left}
  .note{font-size:.88rem;color:var(--muted);margin-top:6px}
  ul{margin:6px 0 0 18px;padding:0}
  .badge{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:10px;background:#f1f5f9;font-weight:700}
  .fine{font-size:.76rem;color:#6b7280;margin-top:10px}
</style>
"""

def sanitize_gpt_html(txt: str) -> str:
    if not txt: return ""
    txt = re.sub(r'^\s*```html\s*', '', txt.strip(), flags=re.IGNORECASE)
    txt = re.sub(r'^\s*```\s*', '', txt, flags=re.IGNORECASE)
    txt = re.sub(r'\s*```\s*$', '', txt)
    return txt.strip()

def extract_grade_and_html(gpt_text: str):
    if not gpt_text: return None, ""
    cleaned = sanitize_gpt_html(gpt_text)
    lines = [l for l in cleaned.splitlines() if l.strip() != ""]
    grade = None
    if lines and lines[0].upper().startswith("GRADE:"):
        m = re.match(r'(?i)grade:\s*([A-F])', lines[0].strip())
        if m: grade = m.group(1).upper()
        body = "\n".join(lines[1:])
    else:
        body = cleaned
    return grade, body

def wrap_report_html(address, content_html, language="en", grade=None):
    rubric = ("A=Outstanding value; B=Good; C=Fair/market; D=Below avg; F=Poor/overpriced."
              if language == "en"
              else "A=Valor excepcional; B=Bueno; C=Justo/mercado; D=Debajo del promedio; F=Malo/sobreprecio.")
    disclaimer = ("This report is informational only and not financial advice."
                  if language == "en"
                  else "Este informe es solo informativo y no constituye asesoramiento financiero.")
    addr = address_to_string(address)
    chip_label = (f"Grade: {grade}" if language == "en" else f"Calificación: {grade}") if grade else ""
    return f"""
{report_wrapper_css()}
<link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">
<div class="wrap" id="deal-report">
  <div class="hdr">
    <span class="material-icons icon">home</span>
    <div class="title">{addr}</div>
    <div class="chip"><span class="material-icons">insights</span>{chip_label}</div>
  </div>
  {content_html}
  <div class="fine">{rubric}<br>{disclaimer}</div>
</div>
"""

# ---------- OpenAI report ----------
@app.route("/clicked", methods=["POST"])
def clicked():
    data = request.get_json() or {}
    address = address_to_string(data.get("address","No address"))
    price = data.get("price","No price")
    bedrooms = data.get("bedrooms","N/A")
    bathrooms = data.get("bathrooms","N/A")
    last_sold_amount = data.get("last_sold_amount","N/A")
    lat = float(data.get("lat", 27.9506)); lon = float(data.get("lon", -82.4572))
    language = data.get("language","en")

    log(f"[OPENAI]/clicked address={address} price={price} lat={lat} lon={lon} lang={language}")

    subject_features = data.get("features") or []
    hoa = data.get("hoa") or ""
    hoa_freq = data.get("hoa_freq") or ""
    cdd = data.get("cdd") or ""

    def price_num(p):
        try: return float(str(p).replace('$','').replace(',',''))
        except Exception: return 0.0
    subject_price = price_num(price)

    def comps_with_radius(r_delta):
        SW_LAT = lat - r_delta; SW_LNG = lon - r_delta
        NE_LAT = lat + r_delta; NE_LNG = lon + r_delta
        location_for_comps = get_zip_or_city(address)
        props = fetch_homes(location_for_comps, SW_LAT, SW_LNG, NE_LAT, NE_LNG)
        comps_local = [h for h in props if h['address'] != address and h['price'] != 'No price']
        comps_sorted = sorted(comps_local, key=lambda h: abs(price_num(h['price']) - subject_price))
        return comps_sorted

    radius_steps = [0.03, 0.05, 0.08]
    comps_sorted = []
    for r in radius_steps:
        comps_sorted = comps_with_radius(r)
        if len(comps_sorted) >= 5:
            log(f"[COMPS] using radius={r} deg with {len(comps_sorted)} comps")
            break
    comps_sorted = comps_sorted[:12]

    comps_text = "\n".join(
        f"{h['address']} | {h['bedrooms']} bd/{h['bathrooms']} ba | {h['price']} | Last Sold: {h['last_sold_amount']}"
        for h in comps_sorted
    ) if comps_sorted else "No close comps found."

    feature_lines = "\n".join(f"- {f}" for f in subject_features) if subject_features else "None listed."
    hoa_line = (f"HOA: {hoa} {('(' + hoa_freq + ')') if hoa_freq else ''}".strip()) if hoa else "HOA: N/A"
    cdd_line = f"CDD: {cdd}" if cdd else "CDD: N/A"
    details = f"Beds: {bedrooms}, Baths: {bathrooms}, Asking Price: {price}, Last Sold Amount: {last_sold_amount}\n{hoa_line} | {cdd_line}\nFeatures:\n{feature_lines}"

    if language == "es":
        sys_text = "Responde SIEMPRE en español. Devuelve la primera línea como 'GRADE: X' y el resto como HTML solamente."
        prompt = f"""
Devuelve en la PRIMERA línea SOLO 'GRADE: X' (X ∈ A,B,C,D,F). En las líneas siguientes devuelve SOLO HTML (sin <html>/<head>).

Propiedad: {address} (lat {lat}, lon {lon})
Datos:
{details}

Comparables cercanos (al menos 5 si hay suficientes; puedes ir más lejos si hace falta):
{comps_text}

REQUISITOS:
- Sección de comparables (.table) con ≥5 filas cuando sea posible: Dirección, BD/BA, Precio, Precio/ft² (si infieres), Última venta (si hay).
- Resumen de “cómo se compara”: mediana de comparables, diferencia absoluta y % vs precio del sujeto, percentil del sujeto.
- Calificación de inversión A–F (A=excelente valor, F=malo/sobreprecio) y muéstrala claramente en “Conclusión”.
- Incluir cerca del inicio una sección con características (Features) y HOA/CDD si están disponibles.
- Estructura EXACTA con estas secciones/clases:
<section class="card" id="summary"> ... </section>
<section class="card" id="keyfacts"> ... </section>
<section class="card" id="pnl"> ... </section>
<section class="card" id="financing"> ... </section>
<section class="card" id="rent-comps"> ... </section>
<section class="card" id="sale-comps"> ... </section>
<section class="card" id="neighborhood"> ... </section>
<section class="card" id="risks"> ... </section>
<section class="card" id="bottom-line"> ... </section>
<section class="card" id="next-steps"> ... </section>
"""
    else:
        sys_text = "Always respond in English. Return the first line as 'GRADE: X' and the rest as HTML only."
        prompt = f"""
On the FIRST line return ONLY 'GRADE: X' (X ∈ A,B,C,D,F). On subsequent lines return HTML ONLY (no <html>/<head>).

Property: {address} (lat {lat}, lon {lon})
Details:
{details}

Nearby comps (at least 5 if possible; reach farther if needed):
{comps_text}

REQUIREMENTS:
- Comps section (.table) with ≥5 rows when possible: Address, BD/BA, Price, Price/ft² (if inferred), Last Sold (if available).
- “How it stacks up”: comps median, absolute & % delta vs subject price, subject price percentile.
- Investment GRADE A–F (A=outstanding value, F=poor/overpriced) shown clearly in Bottom Line.
- Include an early section for Features and HOA/CDD when available.
- Keep EXACT sections/classes:
<section class="card" id="summary"> ... </section>
<section class="card" id="keyfacts"> ... </section>
<section class="card" id="pnl"> ... </section>
<section class="card" id="financing"> ... </section>
<section class="card" id="rent-comps"> ... </section>
<section class="card" id="sale-comps"> ... </section>
<section class="card" id="neighborhood"> ... </section>
<section class="card" id="risks"> ... </section>
<section class="card" id="bottom-line"> ... </section>
<section class="card" id="next-steps"> ... </section>
"""

    try:
        start = time.time()
        resp = oi_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": sys_text},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2200,
            temperature=0.25
        )
        took = time.time() - start
        raw = resp.choices[0].message.content or ""
        log(f"[OPENAI] completion ok in {took:.2f}s, chars={len(raw)}")
        grade, body_html = extract_grade_and_html(raw)
        full_html = wrap_report_html(address, body_html, language, grade=grade)
    except Exception as e:
        log(f"[OPENAI][ERR] {e}")
        full_html = wrap_report_html(
            address,
            "<section class='card'><h3><span class='material-icons'>report_problem</span> Error</h3><div class='note'>Report unavailable.</div></section>",
            language, grade=None
        )

    return jsonify({"status":"success", "info": f"{address},{price}", "report": full_html})

# ---------- Health ----------
@app.get("/debug/ping")
def ping():
    return jsonify({"ok": True, "ts": datetime.now(timezone.utc).isoformat()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
