from openai import OpenAI
import http.client
import json, os
import re
from flask import Flask, render_template, request, jsonify
import folium
import requests
from urllib.parse import quote
from datetime import datetime

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
        # Extract last sold price from possible fields
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

@app.route("/")
def index():
    start_coords = (27.9506, -82.4572)
    folium_map = folium.Map(location=start_coords, zoom_start=12)
    map_html = folium_map.get_root().render()
    return render_template("base.html", map_html=map_html)

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
    # Remove stray plain text "thumb_up Pros & Cons" not inside header
    report_html = re.sub(
        r'(?<!<h3><span class="material-icons">thumb_up</span>)\s*thumb_up Pros & Cons\s*',
        '',
        report_html,
        flags=re.IGNORECASE
    )
    # Replace any "thumb_up Pros & Cons" headers with "compare" icon
    report_html = re.sub(
        r'<h3><span class="material-icons">thumb_up</span>\s*Pros\s*&\s*Cons</h3>',
        r'<h3><span class="material-icons">compare</span> Pros & Cons</h3>',
        report_html,
        flags=re.IGNORECASE
    )
    # Remove duplicate "Pros & Cons" headers if any
    report_html = re.sub(
        r'(<h3><span class="material-icons">compare</span> Pros & Cons</h3>\s*){2,}',
        r'<h3><span class="material-icons">compare</span> Pros & Cons</h3>\n',
        report_html,
        flags=re.IGNORECASE
    )
    return report_html

@app.route("/clicked", methods=["POST"])
def clicked():
    data = request.get_json()
    address = data.get("address", "No address")
    price = data.get("price", "No price")
    bedrooms = data.get("bedrooms", "N/A")
    bathrooms = data.get("bathrooms", "N/A")
    last_sold_amount = data.get("last_sold_amount", "N/A")

    grade = "B"
    details = f"Type: Single Family, Size: 1800 sqft, Beds: {bedrooms}, Baths: {bathrooms}, Upgrades: solar panels, new roof"
    comparables = "101 Elm St | 1700 | $410,000 | Sold last month\n202 Oak Ave | 1900 | $430,000 | Recently renovated"
    assumptions = "Tax 1.2%, Insurance $120/mo, HOA $50/mo"
    financing_assumptions = "Rates: Conventional 7.0%, FHA 6.5%, VA 6.5%. Credit: 700+. FHA/VA rules: as standard. Points: 0"
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    prompt = f"""
Create a property report in HTML format styled as a clean, printable “report card” for prospective real estate buyers with no experience. Use simple, layman’s terms but keep accuracy.

Required Sections & Order:

Header — property address, listed price, overall letter grade (A–F).

Location & Neighborhood — commute times, schools, nearby amenities, neighborhood feel.

Property Details — type, size, beds/baths, notable upgrades (e.g., solar, roof).

Market Snapshot & Comparables — a 4-column table (address, sq ft, sold price, notes) + a short plain-English comparison/interpretation.

Condition & Maintenance — roof/systems status, yard, known items to confirm.

Monthly Cost Snapshot (Est.) — mortgage, utilities, HOA (if any).

Pros & Cons — side-by-side boxes (green for pros, red for cons). 
Use a single header above both boxes: 
<h3><span class="material-icons">compare</span> Pros & Cons</h3>
Inside this section, show two boxes: 
<div class="pros-cons-container">
  <div class="pros-box"><h3><span class="material-icons">thumb_up</span> Pros</h3>...</div>
  <div class="cons-box"><h3><span class="material-icons">warning</span> Cons</h3>...</div>
</div>
Do NOT repeat or add more than one header for pros/cons in this section.

Financing Scenarios (place this section here, above Bottom Line) — use today’s average market rates as of {today_str} and show the math for each loan type below. If you can’t fetch current data, clearly state the assumptions you’re using.

Show Conventional 30-yr (and optionally 15-yr), FHA 30-yr, and VA 30-yr scenarios.

For each scenario, present a clear table with:

Purchase price, down payment %, down payment $

Interest rate (today’s average), APR (if available)

Loan amount (after down payment; for FHA include UFMIP added; for VA include Funding Fee)

Estimated monthly P&I

Mortgage insurance (PMI, FHA MIP, VA $0 MI)

Estimated Taxes, Homeowners Insurance, HOA

Total estimated monthly payment (PITI + MI)

Estimated cash to close (down + closing costs + prepaid items; show total and assumption notes)

Add a plain-English takeaway below the table.

Add a sensitivity note: “Every 0.25% change in rate moves the payment by about $X/month.”

Bottom Line — plain-English summary with Next Steps (ordered list).

Fine Print (small text):

“Scoring: A = Excellent investment; B = Good; C = Average; D = Below average; F = Poor investment.”

“Real estate investments, like all investments, involve inherent risks and are not guaranteed to be profitable. Any information or content provided regarding real estate investment is for informational purposes only and should not be construed as financial, legal, tax, or investment advice.”

Additional Helpful Buyer Sections to Include:

Property Taxes & Insurance Estimates — last year’s taxes + monthly insurance estimate.

Flood Zone & Risk Factors — whether in FEMA flood zone; if yes, show a rough flood-insurance estimate.

Utility Costs & Efficiency — neighborhood utility averages + solar/efficiency features and estimated monthly savings.

Local Market Trends — simple stats (e.g., 12-mo price trend %, avg. days on market).

Homeowner Protections / Warranties — mention availability/value.

Commute & Lifestyle Map — estimated drive times to downtown, airport, hospitals, grocery.

Investment Angle (Optional) — rent estimate and appreciation outlook.

Inspection & Red Flags Checklist — bullets: roof age, HVAC, plumbing, settling, moisture.

Formatting Requirements (HTML + CSS):

Clean, modern card layout; soft background; rounded corners; subtle shadow.

Tables: clean formatting with alternating row colors.

Pros/cons: distinct green/red tinted boxes.

Fine print: smaller, muted gray text at bottom.

Use Google Material Icons instead of emojis.

Load icons:
html
<link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">

> - Example headers:
html
<h3><span class="material-icons">location_on</span> Location & Neighborhood</h3>
<h3><span class="material-icons">home</span> Property Details</h3>
<h3><span class="material-icons">bar_chart</span> Market Snapshot & Comparables</h3>
<h3><span class="material-icons">build</span> Condition & Maintenance</h3>
<h3><span class="material-icons">payments</span> Monthly Cost Snapshot</h3>
<h3><span class="material-icons">compare</span> Pros & Cons</h3>
<h3><span class="material-icons">calculate</span> Financing Scenarios</h3>
<h3><span class="material-icons">task_alt</span> Bottom Line</h3>

Responsiveness requirements:

Include <meta name="viewport" content="width=device-width, initial-scale=1.0">.

Use a fluid grid layout with max-width for report container and % or rem-based spacing.

Make tables scrollable on mobile with overflow-x: auto; display: block;.

Stack side-by-side sections (like Pros/Cons) vertically on small screens using @media (max-width: 768px).

Ensure typography scales with relative units (em/rem) instead of fixed px for better readability.

Input Data (fill placeholders):

Address: {address}

Listed Price: {price}

Grade: {grade}

Property Facts/Upgrades: {details}

Comparables (2–4): {comparables}

Assumptions: {assumptions}

Financing Assumptions: {financing_assumptions}

Last Sold For Amount: {last_sold_amount}
In the report, make a clear comparison between the last sold amount and the current listing price, and discuss what this means for the buyer in terms of value, negotiation leverage, and market trend.
"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000
        )
        raw_report = response.choices[0].message.content
        ai_report = extract_html_from_gpt_response(raw_report)
        ai_report = cleanup_pros_cons_section(ai_report)
    except Exception as e:
        ai_report = f"<div class='report-section'>Error contacting ChatGPT: {str(e)}</div>"

    return jsonify({"status": "success", "info": f"{address},{price}", "report": ai_report})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
