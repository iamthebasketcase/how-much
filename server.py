import asyncio
import json
import os
import time
import uuid

import requests as http
from flask import Flask, jsonify, request, send_file

from scrapers import search_brand

app = Flask(__name__)

_scrape_cache = {}
CACHE_TTL   = 3600
_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
# On Vercel the filesystem is read-only except /tmp
PRICES_FILE     = '/tmp/prices.json'     if os.environ.get('VERCEL') else os.path.join(_BASE_DIR, 'prices.json')
ANALYTICS_FILE  = '/tmp/analytics.json'  if os.environ.get('VERCEL') else os.path.join(_BASE_DIR, 'analytics.json')


# ── Price storage helpers ──────────────────────────────────────────────────────

def _load_prices():
    if os.path.exists(PRICES_FILE):
        with open(PRICES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def _save_prices(prices):
    with open(PRICES_FILE, 'w', encoding='utf-8') as f:
        json.dump(prices, f, indent=2, ensure_ascii=False)

def _load_analytics():
    if os.path.exists(ANALYTICS_FILE):
        with open(ANALYTICS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def _save_analytics(events):
    with open(ANALYTICS_FILE, 'w', encoding='utf-8') as f:
        json.dump(events, f, indent=2, ensure_ascii=False)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    resp = send_file(os.path.join(_BASE_DIR, 'index.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@app.route('/brand_assets/<path:filename>')
def brand_assets(filename):
    return send_file(os.path.join(_BASE_DIR, 'brand_assets', filename))


@app.route('/api/rates')
def get_rates():
    try:
        r     = http.get('https://open.er-api.com/v6/latest/USD', timeout=10)
        data  = r.json()
        rates = {k: data['rates'][k] for k in ('TWD', 'JPY', 'KRW') if k in data.get('rates', {})}
        return jsonify({'base': 'USD', 'rates': rates})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/search', methods=['POST'])
def do_search():
    body  = request.get_json(silent=True) or {}
    brand = body.get('brand', '').lower().strip()
    query = body.get('query', '').strip()

    if not brand or not query:
        return jsonify({'error': 'brand and query are required'}), 400
    if len(query) > 120:
        return jsonify({'error': 'query too long'}), 400
    if brand not in {'lv', 'bottega', 'celine', 'uniqlo', 'gu', 'all'}:
        return jsonify({'error': f'unknown brand: {brand}'}), 400

    key = f'{brand}::{query.lower()}'
    if key in _scrape_cache and time.time() - _scrape_cache[key]['ts'] < CACHE_TTL:
        return jsonify(_scrape_cache[key]['data'])

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            data = loop.run_until_complete(search_brand(brand, query))
        finally:
            loop.close()
        _scrape_cache[key] = {'ts': time.time(), 'data': data}
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Manual price CRUD ──────────────────────────────────────────────────────────

@app.route('/api/prices', methods=['GET'])
def list_prices():
    brand   = request.args.get('brand', '').lower()
    query   = request.args.get('query', '').strip().lower()
    country = request.args.get('country', '').upper()

    prices = _load_prices()
    if brand:
        prices = [p for p in prices if p.get('brand', '').lower() == brand]
    if country:
        prices = [p for p in prices if p.get('country', '').upper() == country]
    if query:
        words  = query.split()
        prices = [p for p in prices
                  if any(w in p.get('productName', '').lower() for w in words)]

    return jsonify(prices)


@app.route('/api/prices', methods=['POST'])
def add_price():
    body = request.get_json(silent=True) or {}
    entry = {
        'id':          str(uuid.uuid4()),
        'brand':       body.get('brand', '').lower().strip(),
        'country':     body.get('country', '').upper().strip(),
        'productName': body.get('productName', '').strip(),
        'price':       body.get('price'),
        'currency':    body.get('currency', '').upper().strip(),
        'query':       body.get('query', '').strip().lower(),
        'date':        body.get('date') or time.strftime('%Y-%m-%d'),
        'timestamp':   int(time.time()),
    }
    if not all([entry['brand'], entry['country'], entry['productName'], entry['price'], entry['currency']]):
        return jsonify({'error': 'brand, country, productName, price, and currency are required'}), 400

    prices = _load_prices()
    prices.append(entry)
    _save_prices(prices)
    return jsonify(entry), 201


@app.route('/api/prices/<price_id>', methods=['DELETE'])
def delete_price(price_id):
    prices = _load_prices()
    prices = [p for p in prices if p.get('id') != price_id]
    _save_prices(prices)
    return jsonify({'ok': True})


# ── Analytics ─────────────────────────────────────────────────────────────────

@app.route('/api/analytics', methods=['POST'])
def track_event():
    body  = request.get_json(silent=True) or {}
    brand = body.get('brand', '').lower().strip()
    query = body.get('query', '').strip()
    if not brand or not query:
        return jsonify({'ok': False}), 400

    event = {
        'brand':     brand,
        'query':     query,
        'date':      time.strftime('%Y-%m-%d'),
        'timestamp': int(time.time()),
    }
    events = _load_analytics()
    events.append(event)
    _save_analytics(events)
    return jsonify({'ok': True}), 201


@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    events = _load_analytics()
    # Aggregate counts per brand
    counts = {}
    for e in events:
        counts[e['brand']] = counts.get(e['brand'], 0) + 1
    return jsonify({'total': len(events), 'by_brand': counts, 'events': events})


if __name__ == '__main__':
    print('LuxPrice → http://localhost:3000')
    app.run(host='0.0.0.0', port=3000, threaded=True)
