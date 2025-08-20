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

**Project by cubantonystark**
