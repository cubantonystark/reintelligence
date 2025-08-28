import http.client
import json
import logging
import os
import pathlib
import re
import time
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from threading import Lock
from urllib.parse import quote

import requests
from flask import Flask, jsonify, render_template, request, session
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
TTL_PROP_BY_ZPID      = int(os.getenv("TTL_PROP_BY_ZPID", "2592000"))

ZILLOW_MIN_INTERVAL   = float(os.getenv("ZILLOW_MIN_INTERVAL", "1.5"))
ZILLOW_429_BACKOFF    = float(os.getenv("ZILLOW_429_BACKOFF", "8.0"))


# ---------- Disk persistence for property cache ----------
PROPERTY_CACHE_FILE = pathlib.Path("property_cache.json")

def _load_property_cache_disk():
    try:
        if PROPERTY_CACHE_FILE.exists():
            with PROPERTY_CACHE_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                with _cache_lock:
                    _cache["property_by_zpid"] = data
            log(f"[DISK_CACHE] loaded {len(_cache['property_by_zpid'])} property entries from disk")
    except Exception as e:
        log(f"[DISK_CACHE] load error: {e}")

def _save_property_cache_disk():
    try:
        with _cache_lock:
            data = _cache.get("property_by_zpid", {})
            with PROPERTY_CACHE_FILE.open("w", encoding="utf-8") as f:
                json.dump(data, f)
        log(f"[DISK_CACHE] saved {len(data)} property entries to disk")
    except Exception as e:
        log(f"[DISK_CACHE] save error: {e}")

# ---------- Cache & Rate limiting ----------

# ---------- Disk Cache ----------
CACHE_DIR = os.getenv("CACHE_DIR", "./cache")
PROP_CACHE_DIR = os.path.join(CACHE_DIR, "property_by_zpid")
os.makedirs(PROP_CACHE_DIR, exist_ok=True)

def _prop_disk_path(zpid: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", str(zpid))
    return os.path.join(PROP_CACHE_DIR, f"{safe}.json")

def _prop_disk_write(zpid: str, payload: dict):
    try:
        path = _prop_disk_path(zpid)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": _now(), "payload": payload}, f)
    except Exception as e:
        log(f"[DISK][WRITE][ERR] zpid={zpid} e={e}")

def _prop_disk_read(zpid: str, ttl: int):
    try:
        path = _prop_disk_path(zpid)
        if not os.path.exists(path): return None
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        ts = obj.get("ts", 0)
        if _now() - ts > ttl:
            # expired on disk -> remove
            try: os.remove(path)
            except Exception: pass
            return None
        return obj  # {"ts":..., "payload":...}
    except Exception as e:
        log(f"[DISK][READ][ERR] zpid={zpid} e={e}")
        return None
_cache_lock = Lock()
_cache = {
    "props_by_location": {},
    "zpid_by_query": {},
    "property_by_zpid": {},
}
# Load existing property cache at startup
_load_property_cache_disk()
_last_request_monotonic = 0.0
_cooldown_until = 0.0
def _now(): return time.time()

def _cache_get(bucket: str, key: str, ttl: int):
    try:
        with _cache_lock:
            entry = _cache[bucket].get(key)
        if not entry:
            log(f"[CACHE][MISS] {bucket}:{key}")
            return None
        if _now() - entry.get("ts", 0) > ttl:
            log(f"[CACHE][EXPIRED] {bucket}:{key}")
            with _cache_lock:
                _cache[bucket].pop(key, None)
            return None
        log(f"[CACHE][HIT] {bucket}:{key}")
        return entry
    except Exception:
        return None

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
    log(f"[RATE] 429 detected. Cooldown until {_cooldown_until:.2f} (monotonic)")

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
    # try disk
    disk = _prop_disk_read(zpid, TTL_PROP_BY_ZPID)
    if disk:
        payload = disk.get("payload")
        if payload is not None:
            # hydrate memory for faster subsequent lookups
            _cache_set("property_by_zpid", zpid, {"payload": payload})
            return payload
    return None
def _store_property_by_zpid(zpid: str, payload: dict):
    _cache_set("property_by_zpid", zpid, {"payload": payload})
    _prop_disk_write(zpid, payload)
    _save_property_cache_disk()

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

    # --- Preload cached properties (<= TTL_PROP_BY_ZPID) that fall within bounds ---
    def _within_bounds(latv, lngv, sw_lat, sw_lng, ne_lat, ne_lng):
        try:
            if sw_lat is None or sw_lng is None or ne_lat is None or ne_lng is None:
                return True
            return (latv >= sw_lat) and (latv <= ne_lat) and (lngv >= sw_lng) and (lngv <= ne_lng)
        except Exception:
            return False

    cached_first = []
    try:
        with _cache_lock:
            for _zpid_key, _entry in (_cache.get("property_by_zpid") or {}).items():
                _payload = (_entry or {}).get("payload") or {}
                _lat = (_payload.get("latitude") or _payload.get("lat") or (_payload.get("property") or {}).get("latitude"))
                _lon = (_payload.get("longitude") or _payload.get("lng") or (_payload.get("property") or {}).get("longitude"))
                if _lat is None or _lon is None: 
                    continue
                try:
                    _lat = float(_lat); _lon = float(_lon)
                except Exception:
                    continue
                if not _within_bounds(_lat, _lon, sw_lat, sw_lng, ne_lat, ne_lng):
                    continue

                _address = (_payload.get("address") or _payload.get("streetAddress") or (_payload.get("property") or {}).get("address") or "")
                _price = (_payload.get("price") or _payload.get("priceRaw") or "")
                _beds = (_payload.get("bedrooms") or (_payload.get("property") or {}).get("bedrooms") or "")
                _baths = (_payload.get("bathrooms") or (_payload.get("property") or {}).get("bathrooms") or "")
                _img = (_payload.get("imgSrc") or _payload.get("img_url") or (_payload.get("property") or {}).get("imgSrc") or "")
                _detail = (_payload.get("detailUrl") or _payload.get("detail_url") or (_payload.get("property") or {}).get("url") or "")
                _last = (_payload.get("lastSoldPrice") or (_payload.get("property") or {}).get("lastSoldPrice") or "N/A")
                try:
                    if _last not in (None, "N/A"):
                        _last = f"${int(float(str(_last).replace('$','').replace(',',''))):,}"
                except Exception:
                    _last = str(_last)

                cached_first.append({
                    "lat": _lat, "lon": _lon, "address": _address, "price": _price,
                    "bedrooms": _beds, "bathrooms": _baths, "img_url": _img, "detail_url": _detail,
                    "last_sold_amount": _last
                })
        if cached_first:
            log(f"[FETCH_HOMES][CACHE_FIRST] {len(cached_first)} cached homes in bounds")
    except Exception as _e:
        cached_first = []
    
    payload = None
    cached = _cache_get("props_by_location", loc_key, TTL_PROPS_BY_LOCATION)
    if cached:
        payload = cached.get("payload")
        # normalize legacy cache shape (list -> dict with props)
        if isinstance(payload, list):
            payload = {"props": payload}
        log(f"[FETCH_HOMES] using cached payload for '{location}'")
    # We'll derive props from cached payload first
    props = (payload.get("props") if isinstance(payload, dict) else (payload if isinstance(payload, list) else []))
    if not props:
        # Try live attempts only if cache had no props
        location_encoded = quote(location)
        attempts = [
            f"/propertyExtendedSearch?location={location_encoded}&status_type=ForSale&home_type=Houses&limit=200",
            f"/propertyExtendedSearch?location={location_encoded}&status_type=ForSale&home_type=AllHomes&limit=200",
            f"/propertyExtendedSearch?location={location_encoded}&home_type=AllHomes&limit=200",
            f"/propertyExtendedSearch?location={location_encoded}&limit=200",
        ]
        status = 0; payload = None
        for idx, path in enumerate(attempts, 1):
            status, payload = _zillow_http_get(path)
            if status != 200 or not isinstance(payload, dict):
                log(f"[FETCH_HOMES][TRY{idx}] status={status}; skipping")
                continue
            props = (payload.get("props") if isinstance(payload, dict) else (payload if isinstance(payload, list) else []))
            log(f"[FETCH_HOMES][TRY{idx}] props={len(props)}")
            if len(props) > 0:
                _cache_set("props_by_location", loc_key, {"payload": (payload if isinstance(payload, dict) else {"props": payload})})
                break
        # If still nothing and we have cached_first (preloaded below), we'll at least return those later.
    props = (payload.get("props") if isinstance(payload, dict) else (payload if isinstance(payload, list) else []))
    log(f"[FETCH_HOMES] raw_props={len(props)} for location={location}")

    listings = list(cached_first)
    seen_keys = set([ (h.get('address') or '').lower() for h in listings ])
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
            last_sold_amount = "N/A"

            # de-dup by address or zpid
        key_addr = (address or '').strip().lower()
        if key_addr in seen_keys:
            continue
        seen_keys.add(key_addr)
        # store lightweight cache by zpid if available
        try:
            zpid_val = str(zpid) if 'zpid' in locals() else str(home.get('zpid') or '')
            if zpid_val:
                _store_property_by_zpid(zpid_val, home)
        except Exception:
            pass
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
_last_results = {"homes": []}
MIN_REFRESH_INTERVAL = float(os.getenv("MIN_REFRESH_INTERVAL", "4.0"))
MIN_CENTER_DELTA_DEG = float(os.getenv("MIN_CENTER_DELTA_DEG", "0.01"))


@app.route("/cache/stats", methods=["GET"])
def cache_stats():
    with _cache_lock:
        stats = {bucket: len(entries) for bucket, entries in _cache.items()}
    return jsonify(stats)

@app.route("/cache/clear", methods=["POST"])
def cache_clear():
    which = (request.get_json() or {}).get("bucket")
    with _cache_lock:
        if which and which in _cache:
            _cache[which].clear()
            if which == "property_by_zpid":
                _save_property_cache_disk()
            return jsonify({"status":"cleared","bucket":which})
        for bucket in _cache:
            _cache[bucket].clear()
        _save_property_cache_disk()
    return jsonify({"status":"cleared_all"})

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
    try:
        _last_results["homes"] = homes
    except Exception:
        pass
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
  /* Force left alignment for all sections (incl. Investment Summary) */
  #deal-report h2, #deal-report h3, #deal-report p, #deal-report ul, #deal-report li { text-align:left !important }
  .addr-meta{ margin:-6px 0 8px 38px; }
  .price-line{ font-size:.95rem; color:var(--muted); margin:2px 0 6px; }
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

def wrap_report_html(address, content_html, language="en", grade=None, price=None):
    rubric = ("A=Outstanding value; B=Good; C=Fair/market; D=Below avg; F=Poor/overpriced."
              if language == "en"
              else "A=Valor excepcional; B=Bueno; C=Justo/mercado; D=Debajo del promedio; F=Malo/sobreprecio.")
    disclaimer = ("This report is informational only and not financial advice."
                  if language == "en"
                  else "Este informe es solo informativo y no constituye asesoramiento financiero.")
    addr = address_to_string(address)
    grade_label = (f"{grade}" if grade else "")

    price_label = ("Asking price" if language == "en" else "Precio de oferta")

    price_line = f"<div class=\"price-line\">{price_label}: {price}</div>" if price not in (None, "", "No price") else ""

    badge_html = ""
    # Start with the model HTML and sanitize duplicates for grade badges/labels
    cleaned_content = content_html if content_html is not None else ""
    # Remove any .badge blocks the model may have inserted
    # (removed) previously stripped .badge blocks to allow model badge to remain
    # Remove lines like "Grade: B" or "Calificación: A" inside common tags
    cleaned_content = re.sub(r'(?is)<(p|div|h[1-6])[^>]*>\s*(grade|calificaci[óo]n)\s*:\s*[A-F][\+\-]?\b.*?</\\\1>', '', cleaned_content)
    # Remove standalone Grade/Calificación lines (plain text)
    cleaned_content = re.sub(r'(?im)^\s*(grade|calificaci[óo]n)\s*:\s*[A-F][\+\-]?\b.*$\n?', '', cleaned_content)



    return f"""
{report_wrapper_css()}
<link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">
<div class="wrap" id="deal-report">
  <div class="hdr">
    <span class="material-icons icon">home</span>
    <div class="title">{addr}</div>
    
  </div>
  <div class="addr-meta">{price_line}</div>
  {cleaned_content}
  <div class="fine">{rubric}<br>{disclaimer}</div>
</div>
"""

# =====================
# Cache Utilities (merged, 30-day TTL)
# =====================
import re as _re_cacheutils
import hashlib as _hashlib_cacheutils
import time as _time_cacheutils
from datetime import datetime as _dt_cacheutils
from typing import Optional as _Optional_cacheutils, Tuple as _Tuple_cacheutils, Dict as _Dict_cacheutils
import os as _os_cacheutils
import json as _json_cacheutils

_CACHE_DIR = "report_cache"
_META_SUFFIX = ".meta.json"
_HTML_SUFFIX = ".html"
_MAX_AGE_DAYS_DEFAULT = 30

def _ensure_cache_dir_cacheutils(path: str = _CACHE_DIR) -> None:
    _os_cacheutils.makedirs(path, exist_ok=True)

def _slugify_cacheutils(text: str) -> str:
    text = text.strip().lower()
    text = _re_cacheutils.sub(r"\s+", " ", text)
    text = _re_cacheutils.sub(r"[^a-z0-9\s,#\-]", "", text)
    text = text.replace(" ", "_").replace(",", "").replace("#", "unit")
    return _re_cacheutils.sub(r"_+", "_", text).strip("_")

def _cache_basename_cacheutils(address: str, lang: _Optional_cacheutils[str] = None) -> str:
    slug = _slugify_cacheutils(address)
    lang_part = f".{lang.lower()}" if lang else ""
    return f"{slug}{lang_part}"

def _html_path_cacheutils(address: str, lang: _Optional_cacheutils[str] = None) -> str:
    _ensure_cache_dir_cacheutils()
    return _os_cacheutils.path.join(_CACHE_DIR, _cache_basename_cacheutils(address, lang) + _HTML_SUFFIX)

def _meta_path_cacheutils(address: str, lang: _Optional_cacheutils[str] = None) -> str:
    _ensure_cache_dir_cacheutils()
    return _os_cacheutils.path.join(_CACHE_DIR, _cache_basename_cacheutils(address, lang) + _META_SUFFIX)

def _now_ts_cacheutils() -> float:
    return _time_cacheutils.time()

def _is_fresh_cacheutils(ts: float, max_age_days: int) -> bool:
    return (_now_ts_cacheutils() - ts) <= max_age_days * 86400

def save_report_to_cache(address: str, html: str, lang: _Optional_cacheutils[str] = None, extras: _Optional_cacheutils[_Dict_cacheutils] = None) -> str:
    """Persist raw HTML and metadata. Returns the HTML file path. (Merged utility)"""
    _ensure_cache_dir_cacheutils()
    html_file = _html_path_cacheutils(address, lang)
    meta_file = _meta_path_cacheutils(address, lang)
    with open(html_file, "w", encoding="utf-8") as f:
        f.write(html)
    meta = {
        "address": address,
        "language": lang or "",
        "saved_at": _dt_cacheutils.utcnow().isoformat() + "Z",
        "timestamp": _now_ts_cacheutils(),
        "sha256": _hashlib_cacheutils.sha256(html.encode("utf-8")).hexdigest(),
    }
    if extras:
        meta.update(extras)
    with open(meta_file, "w", encoding="utf-8") as f:
        _json_cacheutils.dump(meta, f, ensure_ascii=False, indent=2)
    return html_file

def load_cached_report(address: str, lang: _Optional_cacheutils[str] = None, max_age_days: int = _MAX_AGE_DAYS_DEFAULT) -> _Optional_cacheutils[_Tuple_cacheutils[str, _Dict_cacheutils]]:
    """Return (html, meta) if a fresh cached report exists; else None. (Merged utility)"""
    html_file = _html_path_cacheutils(address, lang)
    meta_file = _meta_path_cacheutils(address, lang)
    if not (_os_cacheutils.path.exists(html_file) and _os_cacheutils.path.exists(meta_file)):
        return None
    try:
        with open(meta_file, "r", encoding="utf-8") as f:
            meta = _json_cacheutils.load(f)
        if not _is_fresh_cacheutils(meta.get("timestamp", 0), max_age_days):
            return None
        with open(html_file, "r", encoding="utf-8") as f:
            html = f.read()
        sha = _hashlib_cacheutils.sha256(html.encode("utf-8")).hexdigest()
        if sha != meta.get("sha256"):
            return None
        return html, meta
    except Exception:
        return None

def get_or_generate_report(address: str, lang: _Optional_cacheutils[str], generator_func, *gen_args, max_age_days: int = _MAX_AGE_DAYS_DEFAULT, **gen_kwargs):
    """
    If a fresh cache exists, return it.
    Otherwise, call generator_func(address=..., lang=..., *gen_args, **gen_kwargs) which must
    return a dict with at least {"html": "..."} and optional metadata (e.g., grade, price).
    The result is cached and returned as (html, meta).
    (Merged utility)
    """
    cached = load_cached_report(address, lang, max_age_days=max_age_days)
    if cached:
        return cached  # (html, meta)

    result = generator_func(address=address, lang=lang, *gen_args, **gen_kwargs)
    if not isinstance(result, dict) or "html" not in result:
        raise ValueError("generator_func must return a dict with an 'html' key.")
    html = result["html"]
    extras = {k: v for k, v in result.items() if k != "html"}
    save_report_to_cache(address, html, lang=lang, extras=extras)
    maybe = load_cached_report(address, lang, max_age_days=max_age_days)
    return maybe if maybe else (html, extras)

# End of Cache Utilities
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
    language = (data.get("language","en") or "en").strip().lower()
    language = "es" if language.startswith("es") else ("en" if language.startswith("en") else "en")
    # --- Cache-first: if a fresh report exists for this address+language, serve it immediately ---
    try:
        cached = load_cached_report(address, lang=language)
    except Exception as _e:
        cached = None
    if cached:
        cached_html, cached_meta = cached
        # Wrap the cached body HTML with the shell so the UI/printing looks consistent
        grade_cached = cached_meta.get("grade")
        full_cached = wrap_report_html(address, cached_html, language, grade=grade_cached, price=price)
        try:
            _rid = data.get('zpid') or address
            register_report_consumption(str(_rid))
        except Exception:
            pass
        return jsonify({"status":"success", "info": f"{address},{price}", "report": full_cached, "html": full_cached})
    
    

    log(f"[OPENAI]/clicked address={address} price={price} lat={lat} lon={lon} lang={language}")

    subject_features = data.get("features") or []
    hoa = data.get("hoa") or ""
    hoa_freq = data.get("hoa_freq") or ""
    cdd = data.get("cdd") or ""

    def price_num(p):
        try:
            return float(str(p).replace('$','').replace(',',''))
        except Exception:
            return 0.0

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

    # Build HTML table for provided comparables/benchmarks
    def _esc(x):
        return (str(x).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;') if x is not None else '')
    def _comps_table_html_from_sorted(items):
        if not items:
            return "<p><i>No nearby comparables available from source at this time.</i></p>"
        rows = [
            "<table><thead><tr><th>Address</th><th>Beds</th><th>Baths</th><th>Price</th><th>Last Sold</th><th>Link</th></tr></thead><tbody>",
        ]
        for h in items:
            addr=_esc(h.get('address',''))
            beds=_esc(h.get('bedrooms',''))
            baths=_esc(h.get('bathrooms',''))
            price=_esc(h.get('price',''))
            sold=_esc(h.get('last_sold_amount',''))
            link=(f"https://www.zillow.com{h.get('detail_url','')}" if h.get('detail_url') else '')
            link_html=(f"<a href='{_esc(link)}' target='_blank'>View</a>" if link else '')
            rows.append(f"<tr><td>{addr}</td><td>{beds}</td><td>{baths}</td><td>{price}</td><td>{sold}</td><td>{link_html}</td></tr>")
        rows.append("</tbody></table>")
        return ''.join(rows)
    comps_table_html = _comps_table_html_from_sorted(comps_sorted)


    comps_text = "\n".join(
        f"{h['address']} | {h['bedrooms']} bd/{h['bathrooms']} ba | {h['price']} | Last Sold: {h['last_sold_amount']}"
        for h in comps_sorted
    ) if comps_sorted else "No close comps found."

    feature_lines = "\n".join(f"- {f}" for f in subject_features) if subject_features else "None listed."
    hoa_line = (f"HOA: {hoa} {('(' + hoa_freq + ')') if hoa_freq else ''}".strip()) if hoa else "HOA: N/A"
    cdd_line = f"CDD: {cdd}" if cdd else "CDD: N/A"
    facts = {
        'Address': address,
        'Asking Price': str(price),
        'Bedrooms': bedrooms,
        'Bathrooms': bathrooms,
        'Latitude': lat,
        'Longitude': lon,
        'HOA': (hoa_line if hoa_line else 'N/A'),
        'CDD': (cdd_line if cdd_line else 'N/A'),
    }
    details_html = '<ul>' + ''.join([f"<li><b>{k}:</b> {v}</li>" for k,v in facts.items() if v and v!='N/A']) + '</ul>'


    if language == "es":
        sys_text = "Responde SIEMPRE en español. Devuelve la primera línea como 'GRADE: X' y el resto como HTML solamente."
        prompt = f"""Eres un analista de inversiones inmobiliarias. Presenta siempre los datos en un formato **claro y fácil de usar**, con tablas para comparables y financiamiento.  
No inventes datos; apóyate en portales públicos (Zillow, Redfin, Realtor.com, sitios web del condado).

Genera un **Informe de Inversión Inmobiliaria** profesional para la siguiente propiedad sujeto:

Propiedad: {address} (lat {lat}, lon {lon})  
Detalles (hechos):\n{details_html}\n\nComparables proporcionados:\n[COMPARABLES_TABLE]

### Requisitos de salida:
- Devuelve el informe **solo como HTML** (HTML5 válido).  
- Cada encabezado de sección debe incluir un **ícono de la librería Google Material Icons** usando la etiqueta `<span class="material-icons">`.  
  Ejemplo: `<h2><span class="material-icons">home</span> Fundamentos específicos de la propiedad</h2>`.  
- Usa `<h2>` para secciones principales, `<p>` para texto, `<ul>/<li>` para listas con viñetas y `<table>` para comparables y escenarios de financiamiento.  
- El estilo debe ser limpio y moderno, apto para renderizarse en una aplicación web.

### Estructura del informe:

1. **Calificación de Inversión (A–F)**  
   - Muéstrala de forma destacada en una insignia o encabezado con estilo.  
   - Ejemplo: `<div class="badge">Investment Grade: [INVESTMENT_GRADE]</div>`

2. **Fundamentos específicos de la propiedad** (`home`)  
   - Tamaño: [PROPERTY_SIZE]  
   - Precio por pie cuadrado: [PRICE_PER_SQFT]  
   - Condición, distribución, características únicas: [PROPERTY_FEATURES]  
   - Análisis de ventas comparables con al menos 5 comparables en una tabla HTML clara:

3. **Comparables** (`event_list`)  
   [COMPARABLES_TABLE]  

   escribe aquí un breve análisis comparando el precio de venta de la propiedad con los comparables; usa también Zillow y Redfin o cualquier MLS disponible para extraer el **último precio de venta** de la propiedad y calcula el **porcentaje de diferencia** entre el precio de venta actual y el último precio de venta.

4. **Ubicación y vecindario** (`location_on`)  
   - Escuelas: [SCHOOL_INFO]  
   - Crimen/Seguridad: [CRIME_INFO]  
   - Amenidades y tendencias de desarrollo: [NEIGHBORHOOD_INFO]  
   - Zona de inundación o riesgos naturales (usa mapas de FEMA y del condado)

5. **Indicadores financieros** (`attach_money`)  
   - Valoraciones automatizadas (Zestimate, Redfin)  
   - Impuestos estimados: [ESTIMATED_TAXES]  
   - Seguro: [INSURANCE_COST]  
   - Rango de renta: [RENT_RANGE]  
   - NOI: [NOI]  
   - Tasa de capitalización (Cap Rate): [CAP_RATE]  
   - Retorno efectivo sobre efectivo (Cash-on-Cash): [COC_RETURN]

6. **Escenarios de financiamiento** (`calculate`)  
   - Tabla comparativa para: Convencional 20%, Convencional 5% (PMI), FHA 3.5% (MIP), VA 0% (cuota de financiamiento).  
   - La tabla debe incluir: Enganche, Monto del préstamo, Mensualidad PI, PMI/MIP y PITI estimado a 6.25%, 6.75%, 7.25%.  
   - Inserta la tabla formateada aquí: [FINANCING_TABLE]

7. **Indicadores de riesgo** (`warning`)  
   - Vacancia: [VACANCY_RISK]  
   - Seguro/Inundación: [INSURANCE_RISK]  
   - Inspección/Condición: [INSPECTION_RISK]  
   - Restricciones de HOA: [HOA_RISK]  
   - Ciclo de mercado: [MARKET_CYCLE]

8. **Estrategia de salida** (`trending_up`)  
   - Potencial de reventa: [RESALE_POTENTIAL]  
   - Flexibilidad de renta: [RENTAL_FLEXIBILITY]  
   - Perspectiva de apreciación: [APPRECIATION_OUTLOOK]

9. **Próximos pasos / Lista de verificación de debida diligencia** (`checklist`)  
   - Lista con viñetas: [CHECKLIST_ITEMS]

10. **Justificación de la calificación** (`grading`)  
   - Viñetas: [GRADE_RATIONALE]

11. **Terminar con un breve resumen** (`overview_key`)  
    [INVESTMENT_SUMMARY]

### Pautas de estilo:
- HTML limpio y semántico con encabezados, tablas y listas con viñetas.  
- Explicaciones aptas para principiantes.

"""
        # Inject concrete comparables so the model does NOT invent them
        prompt = prompt.replace('[COMPARABLES_TABLE]', comps_table_html)
        prompt += "\n\nNOTE: Use only the comparables provided above; do not fabricate addresses or prices."

    else:
        sys_text = "Always respond in English. Return the first line as 'GRADE: X' and the rest as HTML only."
        prompt = f"""You are a real estate investment analyst. Always present data in a clear, **user-friendly format** with tables for comparables and financing.   
Do not hallucinate; rely on public portals (Zillow, Redfin, Realtor.com, county websites). 

Generate a professional **Property Investment Report** for the following subject property:

Property: {address} (lat {lat}, lon {lon})
Details (facts):\n{details_html}\n\nComparables Provided:\n[COMPARABLES_TABLE]

### Output requirements:
- Return the report **only as HTML** (valid HTML5).  
- Each section heading should include an **icon from Google’s Material Icons library** using the `<span class="material-icons">` tag.  
  Example: `<h2><span class="material-icons">home</span> Property-Specific Fundamentals</h2>`.  
- Use `<h2>` for main sections, `<p>` for body text, `<ul>/<li>` for bullet points, and `<table>` for comparables and financing scenarios.  
- Style should be clean and modern, suitable for rendering in a web app.  

### Report structure:

1. **Investment Grade (A–F)**  

   - Show prominently at the top in a badge or styled header.  
   - Example: `<div class="badge">Investment Grade: [INVESTMENT_GRADE]</div>`  

2. **Property-Specific Fundamentals** (`home`)  

   - Size: [PROPERTY_SIZE]  
   - Price per square foot: [PRICE_PER_SQFT]  
   - Condition, layout, unique features: [PROPERTY_FEATURES]  
   - Comparable sales analysis with at least 5 comps in a clean HTML table: 

3. **Comparables** (`event_list`)
 
     [COMPARABLES_TABLE]  

     write here a short analysis comparing the property’s asking price to comps, also use Zillow and redfin or any available MLS to extract the last sold price of the property and calculate the percentage difference between the current asking price and the last sold price.

4. **Location & Neighborhood** (`location_on`)  

   - Schools: [SCHOOL_INFO]  
   - Crime/Safety: [CRIME_INFO]  
   - Amenities & Development Trends: [NEIGHBORHOOD_INFO]  
   - Flood zone or natural hazard exposure (use FEMA and county maps)

5. **Financial Indicators** (`attach_money`) 

   - Automated valuations (Zestimate, Redfin)
   - Estimated Taxes: [ESTIMATED_TAXES]  
   - Insurance: [INSURANCE_COST]  
   - Rent Range: [RENT_RANGE]  
   - NOI: [NOI]  
   - Cap Rate: [CAP_RATE]  
   - Cash-on-Cash Return: [COC_RETURN]  

6. **Financing Scenarios** (`calculate`)  

   - Side-by-side table for: Conventional 20%, Conventional 5% (PMI), FHA 3.5% (MIP), VA 0% (funding fee).  
   - Table must include: Down Payment, Loan Amount, Monthly PI, PMI/MIP, and Estimated PITI at 6.25%, 6.75%, 7.25%.  
   - Insert formatted table here: [FINANCING_TABLE]  

7. **Risk Indicators** (`warning`)  

   - Vacancy: [VACANCY_RISK]  
   - Insurance/Flood: [INSURANCE_RISK]  
   - Inspection/Condition: [INSPECTION_RISK]  
   - HOA Restrictions: [HOA_RISK]  
   - Market Cycle: [MARKET_CYCLE]  

8. **Exit Strategy** (`trending_up`)  

   - Resale Potential: [RESALE_POTENTIAL]  
   - Rental Flexibility: [RENTAL_FLEXIBILITY]  
   - Appreciation Outlook: [APPRECIATION_OUTLOOK]  

9. **Next Steps / Due Diligence Checklist** (`checklist`)  

   - Bullet list: [CHECKLIST_ITEMS]  

10. **Grade Rationale** (`grading`)  

   - Bullet points: [GRADE_RATIONALE]  

11- **End with a short summary** (`overview_key`)

    [INVESTMENT_SUMMARY] 

### Style guidelines:

- Clean, semantic HTML with headings, tables, and bullet points.  
- Beginner-friendly explanations.        
"""
        # Inject concrete comparables so the model does NOT invent them
        prompt = prompt.replace('[COMPARABLES_TABLE]', comps_table_html)
        # Strong instruction to avoid fabricating comps
        prompt += "\n\nNOTE: Use only the comparables provided above; do not fabricate addresses or prices."
    def _is_bad_report(html_text: str, lang: str) -> bool:
        try:
            txt = re.sub(r"<[^>]+>", " ", html_text or "", flags=re.IGNORECASE)
            low = txt.lower()
            phrases = ["information not available", "información no proporcionada", "informacion no proporcionada"]
            hits = sum(low.count(p) for p in phrases)
            if not txt.strip(): return True
            if hits >= 3: return True
            ratio = (sum(low.count(p) * len(p) for p in phrases) / max(1, len(low)))
            return ratio >= 0.20
        except Exception:
            return False

    max_attempts = 3
    attempt = 0
    body_html = ""
    grade = None
    full_html = ""
    last_err = None

    nudge = ("Avoid writing 'Information not available'. If a field is unknown, omit the line or write 'N/A' once. "
             "Prefer summarizing available facts from the inputs instead of placeholders.")
    nudge_es = ("Evita escribir 'información no proporcionada'. Si un dato es desconocido, omite la línea o usa 'N/A' una sola vez. "
                "Resume los hechos disponibles en lugar de marcadores de posición.")

    while attempt < max_attempts:
        try:
            start = time.time()
            resp = oi_client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": sys_text},
                    {"role": "user", "content": prompt + "\n\n" + (nudge_es if language=='es' else nudge)}
                ],
                max_tokens=8000,
                temperature=0.25
            )
            took = time.time() - start
            raw = resp.choices[0].message.content or ""
            log(f"[OPENAI] completion ok in {took:.2f}s, chars={len(raw)} (attempt {attempt+1}/{max_attempts})")
            grade, body_html = extract_grade_and_html(raw)
            if not _is_bad_report(body_html, language):
                full_html = wrap_report_html(address, body_html, language, grade=grade, price=price)
                try:
                    save_report_to_cache(address, body_html, lang=language, extras={"grade": grade, "asking_price": price})
                except Exception as _e:
                    log(f"[CACHE][WARN] {str(_e)}")
                break
            else:
                attempt += 1
                last_err = "bad_report"
                if attempt >= max_attempts:
                    full_html = wrap_report_html(
                        address,
                        "<section class='card'><h3><span class='material-icons'>report_problem</span> Error</h3><div class='note'>Report unavailable (retry failed).</div></section>",
                        language, grade=None, price=price
                    )
                    break
        except Exception as e:
            last_err = str(e)
            log(f"[OPENAI][ERR] {e}")
            attempt += 1
            if attempt >= max_attempts:
                full_html = wrap_report_html(
                    address,
                    "<section class='card'><h3><span class='material-icons'>report_problem</span> Error</h3><div class='note'>Report unavailable.</div></section>",
                    language, grade=None, price=price
                )
                break

    try:
        _rid = (locals().get('zpid') or data.get('zpid') or address)
        register_report_consumption(str(_rid))
    except Exception:
        pass
    return jsonify({"status":"success", "info": f"{address},{price}", "report": full_html, "html": full_html})

# ---------- Health ----------
@app.get("/debug/ping")
def ping():
    return jsonify({"ok": True, "ts": datetime.now(timezone.utc).isoformat()})

@app.route("/counter", methods=["GET"])
def counter_state():
    try:
        return jsonify({"count": _counter_load(), "max": COUNTER_MAX})
    except Exception as e:
        return jsonify({"count": 0, "max": COUNTER_MAX, "error": str(e)}), 200


def _override_counter_route():
    """Replace the Flask view function bound to '/counter' so that it returns per-user state.
    This preserves existing clients because we still return 'count' and 'max', but adds 'user' and 'is_logged_in'.
    """
    try:
        for rule in app.url_map.iter_rules():
            if str(rule.rule) == "/counter":
                app.view_functions[rule.endpoint] = _counter_state_useraware
                break
    except Exception:
        pass

_override_counter_route()



# ============= Lightweight Auth + Per-User Quota (non-breaking) =============
USERS_DIR = pathlib.Path("users")
USERS_DIR.mkdir(parents=True, exist_ok=True)

if not getattr(app, "secret_key", None):
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
app.permanent_session_lifetime = timedelta(days=30)

def _user_dir(username: str) -> pathlib.Path:
    return USERS_DIR / re.sub(r"[^a-zA-Z0-9_.-]", "_", str(username))

def _user_files(username: str):
    ud = _user_dir(username)
    pw = ud / "password.txt"
    quota = ud / "quota.json"  # {"count": int, "max": int}
    used = ud / "used.json"    # {"used": [ids]}
    ud.mkdir(parents=True, exist_ok=True)
    try:
        if not used.exists():
            used.write_text(json.dumps({"used": []}), encoding="utf-8")
    except Exception:
        pass
    return pw, quota, used

def _user_read_quota(username: str):
    _, quota_file, _ = _user_files(username)
    try:
        data = json.loads(quota_file.read_text(encoding="utf-8"))
        return int(max(0, data.get("count", 0))), int(max(0, data.get("max", 0)))
    except Exception:
        return 0, 0

def _user_write_quota(username: str, count: int, maxv: int):
    _, quota_file, _ = _user_files(username)
    try:
        quota_file.write_text(json.dumps({"count": int(max(0, count)), "max": int(max(0, maxv))}), encoding="utf-8")
    except Exception:
        pass

def _user_read_used(username: str) -> set:
    _, _, used_file = _user_files(username)
    try:
        data = json.loads(used_file.read_text(encoding="utf-8") or "{}")
        return set(data.get("used", []))
    except Exception:
        return set()

def _user_write_used(username: str, s: set):
    _, _, used_file = _user_files(username)
    try:
        used_file.write_text(json.dumps({"used": list(s)}), encoding="utf-8")
    except Exception:
        pass

def _ensure_test_user():
    uname, pw_plain, start_max = "rey", "R34n3l.2025", 5
    pw_file, quota_file, _ = _user_files(uname)
    if not pw_file.exists():
        try: pw_file.write_text(pw_plain, encoding="utf-8")
        except Exception: pass
    try:
        if not quota_file.exists():
            quota_file.write_text(json.dumps({"count": start_max, "max": start_max}), encoding="utf-8")
    except Exception: pass
_ensure_test_user()

def _current_user():
    u = session.get("user")
    return str(u) if u else None

def _current_user_state():
    u = _current_user()
    if not u:
        return {"is_logged_in": False, "user": "Guest", "count": 0, "max": 0}
    cnt, mx = _user_read_quota(u)
    return {"is_logged_in": True, "user": u, "count": cnt, "max": mx}




@app.before_request
def _enforce_auth_quota_for_clicked():
    try:
        if request.path == "/clicked":
            state = _current_user_state()
            if not state["is_logged_in"]:
                return jsonify({"status": "auth_required", "message": "Login required to generate reports."}), 403
            if state["count"] <= 0:
                return jsonify({"status": "quota_exhausted", "message": "You’ve reached your monthly limit. Please upgrade or buy additional reports."}), 403
    except Exception:
        pass




@app.get("/auth/status")
def auth_status():
    return jsonify(_current_user_state())

@app.post("/auth/login")
def auth_login():
    data = request.get_json(silent=True) or {}
    u = (data.get("username") or "").strip()
    p = data.get("password") or ""
    if not u or not p:
        return jsonify({"ok": False, "error": "missing_fields"}), 400
    pw_file, quota_file, _ = _user_files(u)
    if not pw_file.exists():
        return jsonify({"ok": False, "error": "no_such_user"}), 404
    try:
        stored = pw_file.read_text(encoding="utf-8")
    except Exception:
        stored = ""
    if stored != p:
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    session.permanent = True
    session["user"] = u
    cnt, mx = _user_read_quota(u)
    return jsonify({"ok": True, "user": u, "count": cnt, "max": mx})

@app.post("/auth/register")
def auth_register():
    data = request.get_json(silent=True) or {}
    u = (data.get("username") or "").strip()
    p = data.get("password") or ""
    start_max = int(data.get("starting_quota") or 5)
    if not u or not p:
        return jsonify({"ok": False, "error": "missing_fields"}), 400
    pw_file, quota_file, _ = _user_files(u)
    if pw_file.exists():
        return jsonify({"ok": False, "error": "user_exists"}), 409
    try:
        pw_file.write_text(p, encoding="utf-8")
        quota_file.write_text(json.dumps({"count": start_max, "max": start_max}), encoding="utf-8")
    except Exception as e:
        return jsonify({"ok": False, "error": f"fs_error: {e}"}), 500
    session.permanent = True
    session["user"] = u
    return jsonify({"ok": True, "user": u, "count": start_max, "max": start_max})

@app.post("/auth/logout")
def auth_logout():
    try:
        session.pop("user", None)
    except Exception:
        pass
    return jsonify({"ok": True})





# ---- Override register_report_consumption to use per-user quota only ----
try:
    ORIG_register_report_consumption = register_report_consumption
except Exception:
    ORIG_register_report_consumption = None

def register_report_consumption(report_id: str) -> bool:  # override
    try:
        u = _current_user()
        if not u:
            return False
        if not report_id:
            return False
        cnt, mx = _user_read_quota(u)
        if cnt <= 0:
            return False
        _user_write_quota(u, cnt - 1, mx)
        return True
    except Exception:
        return False

        used = _user_read_used(u)
        if report_id in used:
            return True
        cnt, mx = _user_read_quota(u)
        if cnt <= 0:
            return False
        used.add(report_id); _user_write_used(u, used)
        _user_write_quota(u, cnt - 1, mx)
        return True
    except Exception:
        return ORIG_register_report_consumption(report_id) if ORIG_register_report_consumption else False



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)



# --- Cache: make directory absolute + create on import ---
try:
    import os as _os_cacheutils2, pathlib as _pl_cacheutils2
    _BASE_DIR_CACHEUTILS2 = _pl_cacheutils2.Path(__file__).resolve().parent
    _CACHE_DIR_ABS = str(_BASE_DIR_CACHEUTILS2 / _CACHE_DIR)
    if _CACHE_DIR != _CACHE_DIR_ABS:
        _CACHE_DIR = _CACHE_DIR_ABS  # redirect to absolute path
    _ensure_cache_dir_cacheutils()  # create folder on import
except Exception:
    pass


# ---------- Cache Admin Routes ----------
@app.route("/cache/stats", methods=["GET"])
def cache_stats():
    try:
        # count disk files
        try:
            disk_files = os.listdir(PROP_CACHE_DIR)
        except Exception:
            disk_files = []
        resp = {
            "ttl_prop_by_zpid": TTL_PROP_BY_ZPID,
            "in_memory_counts": {
                "property_by_zpid": len(_cache.get("property_by_zpid", {})),
                "zpid_by_query": len(_cache.get("zpid_by_query", {})),
                "props_by_location": len(_cache.get("props_by_location", {})),
            },
            "disk_counts": {
                "property_by_zpid_files": len([f for f in disk_files if f.endswith(".json")]),
            }
        }
        return jsonify(resp)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/cache/clear", methods=["POST"])
def cache_clear():
    data = request.get_json(silent=True) or {}
    what = (data.get("what") or "properties").lower()
    cleared = {}
    if what in ("properties", "all"):
        # clear in-memory
        with _cache_lock:
            _cache.get("property_by_zpid", {}).clear()
        # clear disk
        try:
            for fn in os.listdir(PROP_CACHE_DIR):
                if fn.endswith(".json"):
                    try: os.remove(os.path.join(PROP_CACHE_DIR, fn))
                    except Exception: pass
            cleared["property_by_zpid"] = "cleared"
        except Exception as e:
            cleared["property_by_zpid"] = f"error: {e}"
    if what == "all":
        with _cache_lock:
            _cache.get("zpid_by_query", {}).clear()
            _cache.get("props_by_location", {}).clear()
        cleared["others"] = "cleared"
    return jsonify({"status": "ok", "cleared": cleared, "scope": what})



def _counter_state_useraware():
    try:
        st = _current_user_state()
        return jsonify({"count": st["count"], "max": st["max"], "user": st["user"], "is_logged_in": st["is_logged_in"]})
    except Exception as e:
        return jsonify({"count": 0, "max": 0, "user": "Guest", "is_logged_in": False, "error": str(e)}), 200