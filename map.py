from openai import OpenAI
import http.client
import json, os
from flask import Flask, render_template, request, jsonify
import folium

app = Flask(__name__)

os.environ["OPENAI_API_KEY"] = "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"

client = OpenAI()

def fetch_homes(sw_lat=None, sw_lng=None, ne_lat=None, ne_lng=None):
    conn = http.client.HTTPSConnection("zillow-com1.p.rapidapi.com")
    headers = {
        'x-rapidapi-key': "a21f37a14emsh4a6f745b07a0863p130fc3jsnb0ac087aeaa9",
        'x-rapidapi-host': "zillow-com1.p.rapidapi.com"
    }
    url = "/propertyExtendedSearch?location=tampa%2C%20fl&status_type=ForSale&home_type=Houses&limit=500"
    conn.request("GET", url, headers=headers)
    res = conn.getresponse()
    data = res.read()
    listings = []
    try:
        result = json.loads(data.decode("utf-8"))
        for home in result.get('props', []):
            lat = home.get('latitude')
            lon = home.get('longitude')
            address = home.get('address', 'No address')
            price = home.get('price', 'No price')
            bedrooms = home.get('bedrooms', 'N/A')
            bathrooms = home.get('bathrooms', 'N/A')
            img_url = home.get('imgSrc')
            detail_url = home.get('detailUrl')
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
        print(f"Homes returned: {len(listings)}")
    except Exception as e:
        print("Error parsing listings:", str(e))
    return listings

@app.route("/")
def index():
    start_coords = (27.9506, -82.4572)
    homes = fetch_homes()
    folium_map = folium.Map(location=start_coords, zoom_start=12)
    if homes:
        for home in homes:
            popup_html = (
                f"<b>{home['address']}</b><br>"
                f"Price: ${home['price']}<br>"
                f"Beds: {home['bedrooms']} | Baths: {home['bathrooms']}<br>"
                f"<a href='https://www.zillow.com{home['detail_url']}' target='_blank'>View Listing</a><br>"
                f"<img src='{home['img_url']}' width='150'><br>"
                f"<button onclick=\"sendClickedInfo('{home['address']}', '{home['price']}')\">Good deal?</button>"
            )
            folium.Marker(
                [home['lat'], home['lon']],
                popup=folium.Popup(popup_html, max_width=300)
                ).add_to(folium_map)
    else:
        folium.Marker(start_coords, popup="No homes found or API error").add_to(folium_map)
    map_html = folium_map.get_root().render()
    return render_template("base.html", map_html=map_html)

@app.route("/refresh", methods=["POST"])
def refresh():
    bounds = request.get_json()
    sw_lat = bounds.get("sw_lat")
    sw_lng = bounds.get("sw_lng")
    ne_lat = bounds.get("ne_lat")
    ne_lng = bounds.get("ne_lng")
    homes = fetch_homes(sw_lat, sw_lng, ne_lat, ne_lng)
    return jsonify(homes)

@app.route("/clicked", methods=["POST"])
def clicked():
    data = request.get_json()
    address = data.get("address", "No address")
    price = data.get("price", "No price")

    prompt = (
        f"Analyze the property at {address} listed for ${price}. "
        "Is it a good deal based on comparables in the area? "
        "Show me recent comparable sales and run numbers for financing with a VA loan and a conventional loan. "
        "Summarize if buying this property is a good investment and give it an investment grade from a-f."
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000
        )
        chatgpt_text = response.choices[0].message.content
    except Exception as e:
        chatgpt_text = f"Error contacting ChatGPT: {str(e)}"

    return jsonify({"status": "success", "info": f"{address},{price}", "report": chatgpt_text})

if __name__ == "__main__":
    app.run(host="192.168.4.150", port=5000, debug=True)
