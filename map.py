from openai import OpenAI
import http.client
import json, os
import re
from flask import Flask, render_template, request, jsonify
import requests
from urllib.parse import quote
from datetime import datetime, timezone

app = Flask(__name__)

os.environ["OPENAI_API_KEY"] = "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"
client = OpenAI()

def reverse_geocode(lat, lon):
    url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lon}"
    headers = {'User-Agent': 'PropertyMapApp/1.0'}
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.json()
        address_obj = data.get('address', {})
        city = (address_obj.get('city') or address_obj.get('town') or address_obj.get('village') or address_obj.get('county'))
        state = address_obj.get('state')
        postcode = address_obj.get('postcode')
        if postcode:
            location = postcode
        elif city and state:
            location = f"{city}, {state}"
        elif state:
            location = state
        else:
            location = "Tampa, FL"
        return location
    except Exception:
        return "Tampa, FL"

def fetch_homes(location, sw_lat=None, sw_lng=None, ne_lat=None, ne_lng=None):
    conn = http.client.HTTPSConnection("zillow-com1.p.rapidapi.com")
    headers = {
        'x-rapidapi-key': "a21f37a14emsh4a6f745b07a0863p130fc3jsnb0ac087aeaa9",
        'x-rapidapi-host': "zillow-com1.p.rapidapi.com"
    }
    location_encoded = quote(location)
    url = f"/propertyExtendedSearch?location={location_encoded}&status_type=ForSale&home_type=Houses&limit=1000"
    conn.request("GET", url, headers=headers)
    res = conn.getresponse()
    data = res.read()
    listings = []
    result = None
    for encoding in ['utf-8', 'latin1']:
        try:
            text = data.decode(encoding)
            result = json.loads(text)
            break
        except Exception:
            continue
    if result is None:
        try:
            text = data.decode('utf-8', errors='ignore')
            result = json.loads(text)
        except Exception:
            result = {"props": []}
    if not isinstance(result, dict) or 'props' not in result:
        result = {"props": []}
    for home in result.get('props', []):
        lat = home.get('latitude')
        lon = home.get('longitude')
        address = home.get('address', 'No address')
        price = home.get('price', 'No price')
        bedrooms = home.get('bedrooms', 'N/A')
        bathrooms = home.get('bathrooms', 'N/A')
        img_url = home.get('imgSrc')
        detail_url = home.get('detailUrl')
        last_sold_amount = (
            home.get('lastSoldPrice') or
            home.get('last_sold_price') or
            home.get('recentSoldPrice') or
            (home.get('priceHistory', [{}])[-1].get('price') if home.get('priceHistory') and isinstance(home.get('priceHistory'), list) and len(home.get('priceHistory')) > 0 else None)
        )
        if last_sold_amount is not None and last_sold_amount != 'null':
            try:
                last_sold_amount = f"${int(float(last_sold_amount)):,.0f}"
            except Exception:
                last_sold_amount = str(last_sold_amount)
        else:
            last_sold_amount = "N/A"
        if (not address or address == 'No address') and lat is not None and lon is not None:
            address = reverse_geocode(lat, lon)
        if lat is not None and lon is not None:
            if sw_lat is not None and sw_lng is not None and ne_lat is not None and ne_lng is not None:
                if not (sw_lat <= lat <= ne_lat and sw_lng <= lon <= ne_lng):
                    continue
            listings.append({
                'lat': lat,
                'lon': lon,
                'address': address,
                'price': price,
                'bedrooms': bedrooms,
                'bathrooms': bathrooms,
                'img_url': img_url,
                'detail_url': detail_url,
                'last_sold_amount': last_sold_amount
            })
    return listings

def get_zip_or_city(address):
    match = re.search(r'\b\d{5}\b', address)
    if match:
        return match.group(0)
    parts = address.split(',')
    if len(parts) > 1:
        return parts[1].strip()
    return "Tampa"

@app.route("/")
def index():
    return render_template("base.html")

@app.route("/refresh", methods=["POST"])
def refresh():
    bounds = request.get_json()
    sw_lat = bounds.get("sw_lat")
    sw_lng = bounds.get("sw_lng")
    ne_lat = bounds.get("ne_lat")
    ne_lng = bounds.get("ne_lng")
    center_lat = bounds.get("center_lat")
    center_lng = bounds.get("center_lng")
    location = reverse_geocode(center_lat, center_lng)
    homes = fetch_homes(location, sw_lat, sw_lng, ne_lat, ne_lng)
    return jsonify(homes)

def extract_html_from_gpt_response(response_text):
    response_text = re.sub(r'^```html\s*', '', response_text, flags=re.MULTILINE)
    response_text = re.sub(r'^```', '', response_text, flags=re.MULTILINE)
    response_text = re.sub(r'```$', '', response_text, flags=re.MULTILINE)
    match = re.search(r'(<html[\s\S]+)', response_text, re.IGNORECASE)
    if match:
        response_text = response_text[match.start():]
    else:
        idx = response_text.find('<')
        if idx != -1:
            response_text = response_text[idx:]
    response_text = re.split(r'(?:This report card provides|Adjust grades, comparables|For first-time homebuyers|Also, change the report)', response_text, flags=re.IGNORECASE)[0]
    html_end = response_text.lower().find('</html>')
    if html_end != -1:
        response_text = response_text[:html_end+7]
    return response_text.strip()

def cleanup_pros_cons_section(report_html):
    report_html = re.sub(
        r'(?<!<h3><span class="material-icons">thumb_up</span>)\s*thumb_up Pros & Cons\s*',
        '',
        report_html,
        flags=re.IGNORECASE
    )
    report_html = re.sub(
        r'<h3><span class="material-icons">thumb_up</span>\s*Pros\s*&\s*Cons</h3>',
        r'<h3><span class="material-icons">compare</span> Pros & Cons</h3>',
        report_html,
        flags=re.IGNORECASE
    )
    report_html = re.sub(
        r'(<h3><span class="material-icons">compare</span> Pros & Cons</h3>\s*){2,}',
        r'<h3><span class="material-icons">compare</span> Pros & Cons</h3>\n',
        report_html,
        flags=re.IGNORECASE
    )
    return report_html

@app.route("/clicked", methods=["POST"])
def clicked():
    data = request.get_json() or {}
    address = data.get("address", "No address")
    price = data.get("price", "No price")
    bedrooms = data.get("bedrooms", "N/A")
    bathrooms = data.get("bathrooms", "N/A")
    last_sold_amount = data.get("last_sold_amount", "N/A")
    lat = float(data.get("lat", 27.9506))
    lon = float(data.get("lon", -82.4572))

    SW_LAT = lat - 0.03
    SW_LNG = lon - 0.03
    NE_LAT = lat + 0.03
    NE_LNG = lon + 0.03

    location_for_comps = get_zip_or_city(address)
    all_properties = fetch_homes(location_for_comps, SW_LAT, SW_LNG, NE_LAT, NE_LNG)

    comparables_raw = [
        h for h in all_properties
        if h['address'] != address and h['price'] != 'No price'
        and abs(h['lat'] - lat) > 0.001 and abs(h['lon'] - lon) > 0.001
    ]

    if len(comparables_raw) < 2:
        extra_props = fetch_homes(location_for_comps)
        seen = set(h['address'] for h in comparables_raw)
        for h in extra_props:
            if h['address'] not in seen and h['address'] != address and h['price'] != 'No price':
                comparables_raw.append(h)
                seen.add(h['address'])

    def price_num(p):
        try:
            return float(str(p).replace('$','').replace(',',''))
        except Exception:
            return 0

    subject_price = price_num(price)
    comparables_raw = sorted(
        comparables_raw,
        key=lambda h: abs(price_num(h['price']) - subject_price)
    )

    comparables_list = comparables_raw[:4] if len(comparables_raw) >= 2 else comparables_raw

    comparables = "\n".join(
        f"{h['address']} | {h['bedrooms']} bd/{h['bathrooms']} ba | {h['price']} | Last Sold: {h['last_sold_amount']}"
        for h in comparables_list
    ) if comparables_list else "No good comparables found nearby."

    details = f"Beds: {bedrooms}, Baths: {bathrooms}, Asking Price: {price}, Last Sold Amount: {last_sold_amount}"

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = f"""
Create a property report in HTML format styled as a clean, printable “report card” for prospective real estate buyers with no experience. Use simple, layman’s terms but keep accuracy.

Required Sections & Order

Header — property address, listed price, and an overall letter grade (A–F) which you must calculate and assign based on the facts provided.

Grade Explanation — add a section explaining why the property received its grade (A–F). Clearly describe the main factors that influenced this grade, both positive and negative, such as location, price, condition, comparables, and market trends. Use header: <h3><span class="material-icons">grade</span> Grade Explanation</h3>

Location & Neighborhood — commute times, schools, nearby amenities, neighborhood feel.

Property Details — type, size, beds/baths, notable upgrades (e.g., solar panels, roof).

Market Snapshot & Comparables
Show a 4-column table (address, sq ft, sold price, notes) using ONLY the comparable properties provided below.
Below the table, write a short plain-English interpretation of what these comps mean for the property’s value.
Clearly compare last sold amount vs. current listing price, and explain in plain English what this means for buyer value, negotiation leverage, and market trends.

Condition & Maintenance — roof/systems status, yard, and key items to confirm.

Monthly Cost Snapshot (Est.) — mortgage, utilities, HOA (if any).

Pros & Cons Section
Use a single header above both boxes:
<h3><span class="material-icons">compare</span> Pros & Cons</h3>
Inside this section, show two boxes:
<div class="pros-cons-container">
  <div class="pros-box"><h3><span class="material-icons">thumb_up</span> Pros</h3>...</div>
  <div class="cons-box"><h3><span class="material-icons">warning</span> Cons</h3>...</div>
</div>
Do NOT repeat or add more than one header for pros/cons in this section.

Financing Scenario (place above Analyze)
Use today’s average market rates as of {today_str}.
If live rates are unavailable, use industry averages and clearly label them as assumptions.
Show a Conventional 30-yr scenario, present a table with:
Purchase price, down payment %, down payment $
Interest rate (today’s average), APR (if available)
Loan amount (after down payment; include FHA UFMIP or VA Funding Fee if applicable)
Estimated monthly P&I
Mortgage insurance (PMI, FHA MIP, or VA $0 MI)
Estimated Taxes, Homeowners Insurance, HOA
Total estimated monthly payment (PITI + MI)
Estimated cash to close (down + closing costs + prepaid items; show assumptions)
Add a plain-English takeaway under each table (e.g., “FHA allows low down payment but adds mortgage insurance; VA is best for eligible buyers with $0 down and no MI; Conventional may be cheapest long-term with 20% down.”).
Add a sensitivity note: “Every 0.25% change in rate moves the payment by about $X/month.”

Summary — section header. Give a plain-English summary that clearly states whether this is a good investment, average, or poor investment and why, based on the details above. Be explicit and direct in your judgement.

Next Steps — section header. Present an actionable, ordered list of recommendations for the buyer, focusing specifically on what they should pay attention to during a property inspection (e.g., roof age, HVAC, plumbing, foundation, moisture, electrical, pest issues, permit status, neighborhood review, etc). Use this section: <h3><span class="material-icons">list_alt</span> Next Steps</h3>

Additional Helpful Buyer Sections (Optional but Recommended)
Property Taxes & Insurance Estimates — last year’s taxes + monthly insurance estimate (why this matters: ongoing costs impact affordability).
Flood Zone & Risk Factors — note FEMA flood zone status and insurance needs (why this matters: flood insurance may add significant cost).
Utility Costs & Efficiency — average local utilities, energy features, and savings estimate (why this matters: reduces monthly bills).
Local Market Trends — 12-mo price trend %, average days on market (why this matters: shows strength/weakness of market).
Homeowner Protections / Warranties — availability/value of a home warranty.
Commute & Lifestyle Map — drive times to airport, downtown, grocery, hospitals.
Investment Angle — potential rental income and resale outlook.
Inspection & Red Flags Checklist — roof age, HVAC, plumbing, foundation, moisture intrusion.

Formatting Requirements (HTML + CSS)
Clean, modern card layout; soft background; rounded corners; subtle shadow.
Responsive design:
Include <meta name="viewport" content="width=device-width, initial-scale=1.0">.
Center report in a container with max-width: 900px and fluid width.
Use % or rem units for padding, margins, and fonts.
Make tables scrollable on small screens with overflow-x: auto; display: block;.
Stack side-by-side sections (like Pros/Cons) vertically on screens <768px.
Ensure fonts scale well on mobile.
Tables: alternating row colors for readability.
Pros/Cons: distinct green/red tinted boxes.
Fine print: smaller, muted gray text at bottom.
Use Google Material Icons instead of emojis.
Load icons:
<link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">

Example headers:
<h3><span class="material-icons">location_on</span> Location & Neighborhood</h3>
<h3><span class="material-icons">home</span> Property Details</h3>
<h3><span class="material-icons">bar_chart</span> Market Snapshot & Comparables</h3>
<h3><span class="material-icons">build</span> Condition & Maintenance</h3>
<h3><span class="material-icons">payments</span> Monthly Cost Snapshot</h3>
<h3><span class="material-icons">compare</span> Pros & Cons</h3>
<h3><span class="material-icons">calculate</span> Financing Scenario</h3>
<h3><span class="material-icons">task_alt</span> Summary</h3>
<h3><span class="material-icons">grade</span> Grade Explanation</h3>
<h3><span class="material-icons">list_alt</span> Next Steps</h3>

Input Data (replace placeholders)
Address: {address}
Listed Price: {price}
Last Sold Amount: {last_sold_amount}
Property Facts/Upgrades: {details}
Comparables (2–4): {comparables}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5000
        )
        raw_report = response.choices[0].message.content
        print(raw_report)  # Print out the raw ChatGPT output to the console
        ai_report = extract_html_from_gpt_response(raw_report)
        ai_report = cleanup_pros_cons_section(ai_report)
        ai_report = ai_report.strip()
    except Exception as e:
        ai_report = f"<div class='report-section'>Error contacting ChatGPT: {str(e)}</div>"

    return jsonify({"status": "success", "info": f"{address},{price}", "report": ai_report})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
