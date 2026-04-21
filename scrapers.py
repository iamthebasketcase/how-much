"""
Scrapers for luxury brand price data.

Fast path (SKU lookup):
  LV products can be fetched directly from their catalog API using a product
  code (e.g. M12925). This requires no browser and bypasses bot-protection.

Slow path (name search):
  Browser-based scraping via camoufox. All major luxury brand sites use
  Cloudflare or custom bot-protection, so this returns empty results with
  direct store links for manual checking.
"""
import asyncio
import html as html_lib
import json
import re
from urllib.parse import quote_plus

try:
    from curl_cffi import requests as cffi_req
    _CFFI = True
except ImportError:
    _CFFI = False

try:
    from camoufox.async_api import AsyncCamoufox
    _CAMOUFOX = True
except ImportError:
    _CAMOUFOX = False

from playwright.async_api import async_playwright


# ── SKU detection ──────────────────────────────────────────────────────────────

# LV SKU pattern: one uppercase letter + exactly 5 digits  (M12925, N41614 …)
_LV_SKU_RE = re.compile(r'^[A-Z]\d{5}$')

def is_lv_sku(query: str) -> bool:
    return bool(_LV_SKU_RE.match(query.strip().upper()))


# ── LV direct product API (SKU path) ─────────────────────────────────────────

_LV_REGIONS = {
    'TW': ('zht-tw', 'TWD', 'tw'),
    'JP': ('jpn-jp', 'JPY', 'jp'),
    'KR': ('kor-kr', 'KRW', 'kr'),
}

def _lv_product_url(locale: str, sku: str) -> str:
    return f'https://api.louisvuitton.com/api/{locale}/catalog/product/{sku.upper()}'


def fetch_lv_by_sku(sku: str) -> dict:
    """
    Fetch LV price for all 3 regions via the catalog product API.
    Returns a dict keyed by country code, ready to merge into search results.
    """
    results = {}
    sku = sku.strip().upper()

    for country, (locale, currency, sub) in _LV_REGIONS.items():
        base      = f'https://{sub}.louisvuitton.com'
        store_url = base

        try:
            resp = cffi_req.get(
                _lv_product_url(locale, sku),
                impersonate='chrome124',
                headers={
                    'Accept':   'application/json',
                    'Referer':  base + '/',
                },
                timeout=12,
            )

            if resp.status_code != 200:
                results[country] = {
                    'currency': currency, 'products': [],
                    'error': f'API returned {resp.status_code}',
                    'storeUrl': store_url, 'searchUrl': None,
                }
                continue

            d      = resp.json()
            model  = d.get('model') or []
            m0     = model[0] if model else {}
            offers = m0.get('offers') or {}
            spec   = offers.get('priceSpecification') or {}

            name      = d.get('name') or d.get('localizedName') or ''
            price     = spec.get('price')
            currency_ = spec.get('priceCurrency') or currency
            formatted = offers.get('price') or ''
            url_path  = d.get('url') or ''
            full_url  = (base + url_path) if url_path else store_url

            # Image: first additionalProperty value that looks like an image URL
            image = ''
            for prop in (m0.get('additionalProperty') or []):
                v = str(prop.get('value', ''))
                if v.startswith('http') and any(v.lower().endswith(ext) for ext in ('.jpg','.png','.jpeg','.webp')):
                    image = v
                    break
            # Fallback: URL with image path but truncated extension
            if not image:
                for prop in (m0.get('additionalProperty') or []):
                    v = str(prop.get('value', ''))
                    if v.startswith('http') and '/images/' in v:
                        image = v
                        break

            if name and price:
                results[country] = {
                    'currency': currency_,
                    'products': [{
                        'name':           name,
                        'price':          price,
                        'currency':       currency_,
                        'formattedPrice': formatted,
                        'url':            full_url,
                        'image':          image,
                        'sku':            sku,
                    }],
                    'error':     None,
                    'storeUrl':  store_url,
                    'searchUrl': full_url,
                }
            else:
                results[country] = {
                    'currency': currency, 'products': [],
                    'error': 'Product not found or price unavailable',
                    'storeUrl': store_url, 'searchUrl': None,
                }

        except Exception as exc:
            results[country] = {
                'currency': currency, 'products': [],
                'error': str(exc),
                'storeUrl': store_url, 'searchUrl': None,
            }

    return results


def parse_price(s):
    if not s:
        return None
    nums = re.sub(r'[^\d]', '', str(s))
    return int(nums) if nums else None


# ── Bottega Veneta search (searchajax HTML parsing) ───────────────────────────

_BV_BASE = 'https://www.bottegaveneta.com'
_BV_LOCALES = {
    'TW': {'path': '/en-tw', 'country': 'TW', 'currency': 'TWD'},
    'JP': {'path': '/ja-jp', 'country': 'JP', 'currency': 'JPY'},
    'KR': {'path': '/ko-kr', 'country': 'KR', 'currency': 'KRW'},
}
_BV_FMT_SYM = [('NT$', 'TWD'), ('₩', 'KRW'), ('¥', 'JPY')]


def fetch_bv_search(query: str) -> dict:
    results = {}
    for country, cfg in _BV_LOCALES.items():
        path        = cfg['path']
        default_cur = cfg['currency']
        store_url   = f'{_BV_BASE}{path}'
        search_url_ = f'{store_url}/search?q={quote_plus(query)}'
        ajax_url    = (f'{_BV_BASE}{path}/searchajax?q={quote_plus(query)}'
                       '&prefn1=akeneo_employeesSalesVisible&prefv1=false'
                       '&prefn2=akeneo_markDownInto&prefv2=no_season'
                       f'&prefn3=countryInclusion&prefv3={cfg["country"]}')
        try:
            resp = cffi_req.get(
                ajax_url, impersonate='chrome124',
                headers={'Accept': 'text/html', 'X-Requested-With': 'XMLHttpRequest',
                         'Referer': search_url_},
                timeout=15,
            )
            if resp.status_code != 200:
                raise Exception(f'HTTP {resp.status_code}')

            # Product JSON in data-gtmproduct attrs
            gtm_attrs  = re.findall(r'data-gtmproduct="(\{[^"]+\})"', resp.text)
            fmt_prices = re.findall(r'(?:NT\$|¥|₩)\s*[\d,]+', resp.text)
            # Map product id → first Medium image from CDN
            img_map    = {}
            for m in re.finditer(
                r'https://bottega-veneta\.dam\.kering\.com/[^"\'<\s]+/Medium/([A-Z0-9]+)_[^"\'<\s]+\.jpg[^"\'<\s]*',
                resp.text
            ):
                pid = m.group(1)
                if pid not in img_map:
                    img_map[pid] = m.group(0)

            prod_urls  = re.findall(rf'href="({re.escape(path)}/[^"]+\.html)"', resp.text)

            products = []
            for i, raw in enumerate(gtm_attrs):
                try:
                    d = json.loads(html_lib.unescape(raw))
                except Exception:
                    continue
                if not d.get('price') or not d.get('name'):
                    continue

                fmt = fmt_prices[i] if i < len(fmt_prices) else ''
                currency = next((cur for sym, cur in _BV_FMT_SYM if sym in fmt), default_cur)
                if not fmt:
                    sym = {'TWD': 'NT$', 'JPY': '¥', 'KRW': '₩'}.get(default_cur, '')
                    fmt = f'{sym}{d["price"]:,}'

                pid         = d.get('id', '')
                product_url = (_BV_BASE + prod_urls[i]) if i < len(prod_urls) else store_url
                image       = img_map.get(pid.split('_')[0], img_map.get(pid, ''))

                products.append({
                    'name': d['name'].title(), 'price': d['price'], 'currency': currency,
                    'formattedPrice': fmt, 'url': product_url, 'image': image, 'sku': pid,
                })

            results[country] = {
                'currency':  products[0]['currency'] if products else default_cur,
                'products':  products,
                'error':     None if products else 'Product not found — try searching by name',
                'storeUrl':  store_url,
                'searchUrl': search_url_,
            }
        except Exception as exc:
            results[country] = {
                'currency': default_cur, 'products': [],
                'error': str(exc), 'storeUrl': store_url, 'searchUrl': search_url_,
            }
    return results


# ── Celine search (Search-ShowAjax HTML parsing) ─────────────────────────────

_CELINE_BASE = 'https://www.celine.com'
_CELINE_LOCALES = {
    'TW': {'path': '/en-tw', 'sfcc': 'Sites-CELINE_TW-Site/en_TW', 'currency': 'TWD'},
    'JP': {'path': '/ja-jp', 'sfcc': 'Sites-CELINE_JP-Site/ja_JP', 'currency': 'JPY'},
    'KR': {'path': '/ko-kr', 'sfcc': 'Sites-CELINE_KR-Site/ko_KR', 'currency': 'KRW'},
}
_CELINE_FMT_SYM = [('NT$', 'TWD'), ('₩', 'KRW'), ('¥', 'JPY'), ('yen', 'JPY')]


def fetch_celine_search(query: str) -> dict:
    results = {}
    for country, cfg in _CELINE_LOCALES.items():
        path        = cfg['path']
        default_cur = cfg['currency']
        store_url   = f'{_CELINE_BASE}{path}'
        search_url_ = f'{store_url}/search?q={quote_plus(query)}'
        ajax_url    = (f'{_CELINE_BASE}/on/demandware.store/{cfg["sfcc"]}'
                       f'/Search-ShowAjax?q={quote_plus(query)}')
        try:
            resp = cffi_req.get(
                ajax_url, impersonate='chrome124',
                headers={'Accept': 'text/html', 'X-Requested-With': 'XMLHttpRequest',
                         'Referer': search_url_},
                timeout=15,
            )
            if resp.status_code != 200:
                raise Exception(f'HTTP {resp.status_code}')

            text = html_lib.unescape(resp.text)  # resolve &yen; → ¥ etc.
            blocks = re.split(r'(?=<(?:li|div)[^>]+o-listing-grid__item)', text)
            products = []
            for block in blocks[1:9]:
                name_m = re.search(r'm-product-listing__meta-title[^>]*>\s*([^<]+?)\s*<', block)
                price_m = re.search(r'(?:NT\$|¥|₩)\s*[\d,]+', block)
                url_m   = re.search(rf'href="({re.escape(path)}/[^"]+\.html)"', block)
                img_m   = re.search(r'(?:src|data-src)="(https://image\.celine\.com/[^"]+\.(?:jpg|png|webp))[^"]*"', block)

                if not name_m or not price_m:
                    continue

                fmt      = price_m.group(0)
                currency = next((cur for sym, cur in _CELINE_FMT_SYM if sym in fmt), default_cur)
                raw_num  = re.sub(r'[^\d]', '', fmt)
                price    = int(raw_num) if raw_num else None
                name     = name_m.group(1).strip()
                if not price or not name:
                    continue

                products.append({
                    'name':           name.title(),
                    'price':          price,
                    'currency':       currency,
                    'formattedPrice': fmt,
                    'url':            (_CELINE_BASE + url_m.group(1)) if url_m else store_url,
                    'image':          img_m.group(1) if img_m else '',
                    'sku':            '',
                })

            results[country] = {
                'currency':  products[0]['currency'] if products else default_cur,
                'products':  products,
                'error':     None if products else 'Product not found — try searching by name',
                'storeUrl':  store_url,
                'searchUrl': search_url_,
            }
        except Exception as exc:
            results[country] = {
                'currency': default_cur, 'products': [],
                'error': str(exc), 'storeUrl': store_url, 'searchUrl': search_url_,
            }
    return results


COUNTRIES = {
    'TW': {'currency': 'TWD', 'locale': 'zh-TW', 'tz': 'Asia/Taipei'},
    'JP': {'currency': 'JPY', 'locale': 'ja-JP', 'tz': 'Asia/Tokyo'},
    'KR': {'currency': 'KRW', 'locale': 'ko-KR', 'tz': 'Asia/Seoul'},
}

# Direct store links (shown to users when scraping fails)
STORE_URLS = {
    'lv': {
        'TW': 'https://tw.louisvuitton.com/zht-tw',
        'JP': 'https://jp.louisvuitton.com/jpn-jp',
        'KR': 'https://kr.louisvuitton.com/kor-kr',
    },
    'chanel': {
        'TW': 'https://www.chanel.com/tw',
        'JP': 'https://www.chanel.com/jp',
        'KR': 'https://www.chanel.com/kr',
    },
    'dior': {
        'TW': 'https://www.dior.com/zh_tw',
        'JP': 'https://www.dior.com/ja_jp',
        'KR': 'https://www.dior.com/ko_kr',
    },
    'bottega': {
        'TW': 'https://www.bottegaveneta.com/en-tw',
        'JP': 'https://www.bottegaveneta.com/ja-jp',
        'KR': 'https://www.bottegaveneta.com/ko-kr',
    },
    'celine': {
        'TW': 'https://www.celine.com/en-tw',
        'JP': 'https://www.celine.com/ja-jp',
        'KR': 'https://www.celine.com/ko-kr',
    },
}

def search_url(brand, country, query):
    q = quote_plus(query)
    patterns = {
        'lv': {
            'TW': f'https://tw.louisvuitton.com/zht-tw/search#q={q}&t=FilteredSearch',
            'JP': f'https://jp.louisvuitton.com/jpn-jp/search#q={q}&t=FilteredSearch',
            'KR': f'https://kr.louisvuitton.com/kor-kr/search#q={q}&t=FilteredSearch',
        },
        'chanel': {
            'TW': f'https://www.chanel.com/tw/search/?q={q}',
            'JP': f'https://www.chanel.com/jp/search/?q={q}',
            'KR': f'https://www.chanel.com/kr/search/?q={q}',
        },
        'dior': {
            'TW': f'https://www.dior.com/zh_tw/search#query={q}',
            'JP': f'https://www.dior.com/ja_jp/search#query={q}',
            'KR': f'https://www.dior.com/ko_kr/search#query={q}',
        },
        'bottega': {
            'TW': f'https://www.bottegaveneta.com/en-tw/search?q={q}',
            'JP': f'https://www.bottegaveneta.com/ja-jp/search?q={q}',
            'KR': f'https://www.bottegaveneta.com/ko-kr/search?q={q}',
        },
        'celine': {
            'TW': f'https://www.celine.com/en-tw/search?q={q}',
            'JP': f'https://www.celine.com/ja-jp/search?q={q}',
            'KR': f'https://www.celine.com/ko-kr/search?q={q}',
        },
    }
    return patterns.get(brand, {}).get(country)


_DOM_EXTRACT_JS = """
(selectors) => {
    let cards = [];
    for (const sel of selectors) {
        const found = document.querySelectorAll(sel);
        if (found.length > 0) { cards = Array.from(found); break; }
    }
    return cards.slice(0, 8).map(card => {
        const nameEl  = card.querySelector('[class*="name"],[class*="title"],[class*="product-name"],h3,h4,h2');
        const priceEl = card.querySelector('[class*="price"],[class*="Price"],[data-price]');
        const link    = card.querySelector('a');
        const img     = card.querySelector('img');
        const price   = priceEl?.textContent?.trim() || priceEl?.getAttribute('data-price') || '';
        return {
            name:           nameEl?.textContent?.trim() || '',
            formattedPrice: price,
            url:            link?.href || '',
            image:          img?.src   || img?.dataset?.src || '',
        };
    }).filter(p => p.name && p.formattedPrice);
}
"""

BRAND_DOM_SELECTORS = {
    'lv':     ['[class*="lv-product-card"]','[class*="productCard"]','[data-testid="product-card"]'],
    'chanel': ['[class*="product-item"]','[class*="product-card"]','.c-product-grid__item','article[class*="product"]'],
    'dior':   ['[class*="product-card"]','[class*="product-item"]','li[class*="product"]'],
}

async def _dismiss_cookies(page):
    for sel in ['#onetrust-accept-btn-handler','button[id*="accept"]','button[class*="accept"]',
                '[class*="cookie"] button','[id*="cookie"] button']:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(600)
                return
        except Exception:
            pass


async def _scrape_country(browser, brand, country, currency, query):
    url = search_url(brand, country, query)
    if not url:
        return []

    page = await browser.new_page()
    captured = []

    async def on_response(response):
        if response.status != 200:
            return
        ct = response.headers.get('content-type', '')
        if 'json' not in ct:
            return
        rurl = response.url
        try:
            if brand == 'lv' and 'catalog/search' in rurl:
                data = await response.json()
                for r in data.get('records', [])[:8]:
                    name  = r.get('localizedName') or r.get('nameEN') or r.get('name', '')
                    pfmt  = r.get('priceFormatted') or str(r.get('price', ''))
                    price = parse_price(pfmt)
                    image = None
                    models = r.get('models', [])
                    if models:
                        m = models[0] if isinstance(models, list) else models
                        bg = m.get('background', {})
                        image = bg.get('src') or bg.get('cdnSrc')
                    base = STORE_URLS['lv'][country]
                    u = r.get('url', '')
                    full_url = f'{base}{u}' if u and not u.startswith('http') else u
                    if name and price:
                        captured.append({'name': name.strip(), 'price': price, 'currency': currency,
                                         'formattedPrice': pfmt, 'url': full_url, 'image': image})

            elif brand == 'dior' and 'search' in rurl:
                data = await response.json()
                items = (data.get('items') or data.get('products') or
                         data.get('results') or data.get('hits') or [])
                for item in items[:8]:
                    name   = item.get('name') or item.get('title', '')
                    pobj   = item.get('price', {})
                    pfmt   = pobj.get('formatted') if isinstance(pobj, dict) else str(pobj)
                    price  = parse_price(pfmt)
                    image  = item.get('image') or item.get('thumbnail')
                    iurl   = item.get('url') or item.get('link', '')
                    if name and price:
                        captured.append({'name': name.strip(), 'price': price, 'currency': currency,
                                         'formattedPrice': str(pfmt or ''), 'url': iurl, 'image': image})
        except Exception:
            pass

    page.on('response', on_response)

    try:
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=20000)
        except Exception:
            pass

        await _dismiss_cookies(page)
        await page.wait_for_timeout(4000)

        if captured:
            return captured

        # DOM fallback
        selectors = BRAND_DOM_SELECTORS.get(brand, ['[class*="product-card"]'])
        try:
            dom_items = await page.evaluate(_DOM_EXTRACT_JS, selectors)
            for p in dom_items:
                price = parse_price(p.get('formattedPrice'))
                if price and price > 0:
                    captured.append({'name': p['name'], 'price': price, 'currency': currency,
                                     'formattedPrice': p['formattedPrice'],
                                     'url': p.get('url'), 'image': p.get('image')})
        except Exception:
            pass

        return captured

    finally:
        await page.close()


async def _search_single(brand, query):
    # Fast path: LV SKU lookup via catalog API (no browser required)
    if brand == 'lv' and is_lv_sku(query) and _CFFI:
        country_data = fetch_lv_by_sku(query)
        return {'brand': 'lv', 'query': query, **country_data}

    # Fast path: Bottega Veneta searchajax (HTML GTM parsing)
    if brand == 'bottega' and _CFFI:
        country_data = fetch_bv_search(query)
        return {'brand': 'bottega', 'query': query, **country_data}

    # Fast path: Celine Search-ShowAjax (HTML parsing)
    if brand == 'celine' and _CFFI:
        country_data = fetch_celine_search(query)
        return {'brand': 'celine', 'query': query, **country_data}

    results = {}
    q = quote_plus(query)

    launch_kwargs = dict(
        headless=True,
        args=['--no-sandbox', '--disable-setuid-sandbox',
              '--disable-blink-features=AutomationControlled',
              '--disable-dev-shm-usage'],
    )

    async def run_with_browser(BrowserCM):
        async with BrowserCM as browser:
            tasks = []
            for country, cfg in COUNTRIES.items():
                tasks.append((country, cfg['currency'],
                              asyncio.ensure_future(_scrape_country(browser, brand, country, cfg['currency'], query))))

            for country, currency, task in tasks:
                store = STORE_URLS.get(brand, {}).get(country)
                search = search_url(brand, country, query)
                try:
                    products = await task
                    results[country] = {
                        'currency': currency,
                        'products': products,
                        'error':    None,
                        'storeUrl': store,
                        'searchUrl': search,
                    }
                except Exception as e:
                    results[country] = {
                        'currency': currency,
                        'products': [],
                        'error':    str(e),
                        'storeUrl': store,
                        'searchUrl': search,
                    }

    if _CAMOUFOX:
        await run_with_browser(AsyncCamoufox(**launch_kwargs, geoip=False))
    else:
        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_kwargs)
            try:
                # Minimal context per country
                async def scrape_with_pw(country, currency):
                    cfg = COUNTRIES[country]
                    ctx = await browser.new_context(
                        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36',
                        locale=cfg['locale'], timezone_id=cfg['tz'],
                        viewport={'width': 1440, 'height': 900},
                    )
                    try:
                        return await _scrape_country(ctx, brand, country, currency, query)
                    finally:
                        await ctx.close()

                for country, cfg in COUNTRIES.items():
                    store = STORE_URLS.get(brand, {}).get(country)
                    search = search_url(brand, country, query)
                    try:
                        products = await scrape_with_pw(country, cfg['currency'])
                        results[country] = {'currency': cfg['currency'], 'products': products,
                                            'error': None, 'storeUrl': store, 'searchUrl': search}
                    except Exception as e:
                        results[country] = {'currency': cfg['currency'], 'products': [],
                                            'error': str(e), 'storeUrl': store, 'searchUrl': search}
            finally:
                await browser.close()

    return {'brand': brand, 'query': query, **results}


async def search_brand(brand, query):
    if brand == 'all':
        all_results = {}
        for b in ['lv', 'bottega', 'celine']:
            try:
                all_results[b] = await _search_single(b, query)
            except Exception as e:
                all_results[b] = {'brand': b, 'query': query, 'error': str(e)}
        return all_results
    return await _search_single(brand, query)
