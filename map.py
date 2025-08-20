from openai import OpenAI
import http.client
import json, os
import re
from flask import Flask, render_template, request, jsonify
import folium
import requests
from urllib.parse import quote

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
    except Exception as e:
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
        except Exception as e:
            print("Failed to decode/load Zillow API response. Raw response below:")
            print(data)
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
                'detail_url': detail_url
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

@app.route("/clicked", methods=["POST"])
def clicked():
    data = request.get_json()
    address = data.get("address", "No address")
    price = data.get("price", "No price")

    prompt = (
        f"Generate a detailed property investment report for the property at {address}, listed for ${price}.\n"
        "Organize the report into the following sections, using these exact headers:\n"
        "Property Insights\nArea Comparables\nFinancing Scenarios\nSummary\nGrade\n"
        "Output the report in markdown format using section headers and markdown lists/tables as needed. Do not include introductions. For property Insights, just list the property Adress, the listing type and the Type. Use interest rates taht reflect the current landscape."
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000
        )
        ai_report = response.choices[0].message.content
    except Exception as e:
        ai_report = f"Error contacting ChatGPT: {str(e)}"

    return jsonify({"status": "success", "info": f"{address},{price}", "report": ai_report})

if __name__ == "__main__":
    app.run(host="192.168.4.150", port=5000, debug=True)
