"""
Web app (FastAPI) para:
1) Subir un Excel con columna "EAN".
2) Buscar el URL del producto en varios supermercados (Carrefour, Jumbo, Disco, Vea, Día, La Anónima).
3) Devolver un Excel con una columna por supermercado; si no existe, pone "no encontrado".

Cómo correr:

python -m venv .venv && .venv/bin/pip install --upgrade pip
.venv/bin/pip install fastapi uvicorn httpx pandas openpyxl beautifulsoup4
# En Windows: .venv\Scripts\pip install ... (igual que arriba)

# Levantar el server (modo desarrollo):
.venv/bin/uvicorn app:app --reload  # (o uvicorn app:app --reload en Windows)

Abrí http://127.0.0.1:8000/ y probá.
"""
from __future__ import annotations

from uuid import uuid4
import asyncio
import io
import re
from typing import Dict, List, Optional

from urllib.parse import urljoin, urlparse
import json
from functools import lru_cache
import httpx
import pandas as pd
from bs4 import BeautifulSoup
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse

# --------------------------- Configuración ---------------------------
# Supermercados objetivo (podés agregar/quitar sin tocar el resto del código)
STORES: Dict[str, str] = {
    "carrefour": "https://www.carrefour.com.ar",
    "jumbo": "https://www.jumbo.com.ar",
    "disco": "https://www.disco.com.ar",
    "vea": "https://www.vea.com.ar",
    "dia": "https://diaonline.supermercadosdia.com.ar",
    "farmacity": "https://www.farmacity.com",
    "mas_online": "https://www.masonline.com.ar",
    "pigmento": "https://www.perfumeriaspigmento.com.ar",
    "mercado_libre": "https://listado.mercadolibre.com.ar",
    "club_de_beneficios": "https://clubdebeneficios.com",
    "central_oeste": "https://www.centraloeste.com.ar",
    "atomo": "https://atomoconviene.com/atomo-ecommerce",
}

FRIENDLY_NAMES = {
    "carrefour": "Carrefour",
    "jumbo": "Jumbo",
    "disco": "Disco",
    "vea": "Vea",
    "dia": "Día",
    "farmacity": "Farmacity",
    "mas_online": "Más Online",
    "pigmento": "Perfumerías Pigmento",
    "mercado_libre": "Mercado Libre",
    "club_de_beneficios": "Club de Beneficios",
    "central_oeste": "Central Oeste",
    "atomo": "Átomo",
}


# Concurrencia (ajustá si necesitás ser más/menos agresivo)
MAX_PARALLEL_PER_EAN = 6
REQUEST_TIMEOUT = 12.0
PER_STORE_TIMEOUT = 9.0  # segundos: límite duro por tienda/handler

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}

# --------------------------- Utilidades ---------------------------
# URLs que NO son PDP y debemos ignorar
DISALLOWED_PARTS = (
    "/account", "/login", "/orders", "/order", "/cart",
    "/minicart", "/wishlist", "/customer", "#/orders",
    "_q=", "map=ft",
)

PROGRESS: Dict[str, Dict[str, int | str]] = {}
RESULTS: Dict[str, io.BytesIO] = {}

def normalize_base_url(u: str) -> str:
    """Normaliza la URL base de una tienda: agrega https:// si falta y quita barras finales."""
    if not u:
        return ""
    u = u.strip()
    if not u.startswith("http://") and not u.startswith("https://"):
        u = "https://" + u.lstrip("/")
    return u.rstrip("/")

def is_valid_pdp(url: str, base_url: str) -> bool:
    """Acepta solo PDPs reales del mismo host. En VTEX exigimos que el path termine en '/p'."""
    if not url:
        return False
    low = url.lower()
    if any(bad in low for bad in DISALLOWED_PARTS):
        return False
    try:
        bu = urlparse(base_url)
        # normalizar a absoluta
        full = url if url.startswith("http") else urljoin(base_url.rstrip("/") + "/", url)
        uu = urlparse(full)
        # mismo host
        if bu.netloc and bu.netloc not in uu.netloc:
            return False
        # PDP VTEX real: path termina en /p (antes de query/fragment)
        path = uu.path.rstrip("/")
        if not path.endswith("/p"):
            return False
        return True
    except Exception:
        return False

def sanitize_ean(value: str) -> str:
    """Deja sólo dígitos y conserva ceros a la izquierda (como string)."""
    if value is None:
        return ""
    s = str(value).strip()
    # A veces vienen como 7793742007897.0 -> quitar .0 final si Excel lo forzó:
    s = re.sub(r"\.0$", "", s)
    # Quitar cualquier separador raro:
    s = re.sub(r"\D", "", s)
    return s


def build_api_url(base_url: str, ean: str) -> str:
    # VTEX Search API por EAN (alternateIds_Ean)
    return f"{base_url.rstrip('/')}/api/catalog_system/pub/products/search/?fq=alternateIds_Ean:{ean}"


def build_search_page_url(base_url: str, ean: str) -> str:
    # Búsqueda por texto (genérica en VTEX y varios storefronts)
    return f"{base_url.rstrip('/')}/{ean}?_q={ean}&map=ft"

def first_product_link_from_html(html: str, base_url: str) -> Optional[str]:
    """Devuelve el primer enlace a PDP válida (path que termina en '/p') o None."""
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)

    for a in anchors:
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue
        # Normalizar a URL absoluta
        if href.startswith("//"):
            full = "https:" + href
        elif href.startswith("/"):
            full = base_url.rstrip("/") + href
        elif href.startswith("http"):
            full = href
        else:
            full = urljoin(base_url.rstrip("/") + "/", href)

        # Aceptar solo PDP válidas
        if is_valid_pdp(full, base_url):
            return full

    return None

async def fetch_json(client: httpx.AsyncClient, url: str) -> Optional[List[dict]]:
    try:
        r = await client.get(url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            # Algunos endpoints devuelven text/html aunque sea JSON válido;
            # intentar .json() y si falla, None.
            try:
                return r.json()
            except Exception:
                return None
        return None
    except Exception:
        return None


async def fetch_text(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        r = await client.get(url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200 and r.text:
            return r.text
        return None
    except Exception:
        return None

async def lookup_in_store(client: httpx.AsyncClient, store_name: str, base_url: str, ean: str) -> str:
    """VTEX: pruebo por EAN exacto y luego fulltext, y por último HTML."""
    # 1) API por EAN exacto
    api_url = f"{base_url.rstrip('/')}/api/catalog_system/pub/products/search/?fq=alternateIds_Ean:{ean}"
    data = await fetch_json(client, api_url)
    if isinstance(data, list) and data:
        prod = data[0]
        link = prod.get("link") or prod.get("linkText")
        if link:
            if not link.startswith("http"):
                if "/" not in link:
                    link = f"{base_url.rstrip('/')}/{link}/p"
                else:
                    link = f"{base_url.rstrip('/')}/{link}"
            return link
        lt = prod.get("linkText")
        if lt:
            return f"{base_url.rstrip('/')}/{lt}/p"

    # 2) API full-text (algunas tiendas indexan el EAN solo por ft)
    ft_url = f"{base_url.rstrip('/')}/api/catalog_system/pub/products/search/?ft={ean}"
    data = await fetch_json(client, ft_url)
    if isinstance(data, list) and data:
        prod = data[0]
        link = prod.get("link") or prod.get("linkText")
        if link:
            if not link.startswith("http"):
                if "/" not in link:
                    link = f"{base_url.rstrip('/')}/{link}/p"
                else:
                    link = f"{base_url.rstrip('/')}/{link}"
            return link
        lt = prod.get("linkText")
        if lt:
            return f"{base_url.rstrip('/')}/{lt}/p"

    # 3) Página de resultados (HTML)
    search_url = f"{base_url.rstrip('/')}/{ean}?_q={ean}&map=ft"
    html = await fetch_text(client, search_url)
    if html:
        maybe = first_product_link_from_html(html, base_url)
        if maybe and is_valid_pdp(maybe, base_url):
            return maybe

    return "no encontrado"

# ---------------------- Plataforma + Heurísticas genéricas ----------------------

class Platform:
    VTEX = "vtex"
    MAGENTO = "magento"
    PRESTASHOP = "prestashop"
    SHOPIFY = "shopify"
    WOOCOMMERCE = "woocommerce"
    TIENDANUBE = "tiendanube"
    UNKNOWN = "unknown"

BANNED_FRAGMENTS = (
    "/login", "/account", "/customer", "/cart", "/checkout", "/wishlist",
    "/ofertas", "/sale", "/help", "/ayuda"
)

def _host(u: str) -> str:
    try:
        return urlparse(u).netloc
    except Exception:
        return ""

def _abs(base: str, href: str) -> str:
    if not href: return ""
    href = href.strip()
    if href.startswith("//"): return "https:" + href
    if href.startswith("http"): return href
    return urljoin(base.rstrip("/") + "/", href.lstrip("/"))

def _seems_pdp(u: str, platform: str) -> bool:
    low = (u or "").lower()
    if any(x in low for x in BANNED_FRAGMENTS):
        return False
    path = urlparse(low).path

    if platform == Platform.VTEX:
        return path.rstrip("/").endswith("/p")
    if platform == Platform.MAGENTO:
        return path.endswith(".html")
    if platform == Platform.PRESTASHOP:
        # suele haber ID numérico y .html
        return path.endswith(".html")
    if platform == Platform.SHOPIFY:
        return "/products/" in path
    if platform == Platform.WOOCOMMERCE:
        return "/product/" in path and "/product-category/" not in path
    if platform == Platform.TIENDANUBE:
        return "/productos/" in path and "/colecciones/" not in path

    # genérico
    return any(seg in path for seg in ("/product", "/producto", "/products", "/productos")) or path.endswith(".html")

def _jsonld_items(html: str):
    out = []
    try:
        soup = BeautifulSoup(html, "html.parser")
        for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(s.string or s.text or "")
            except Exception:
                continue
            if isinstance(data, dict):
                out.append(data)
            elif isinstance(data, list):
                out.extend([d for d in data if isinstance(d, dict)])
    except Exception:
        pass
    return out

def _first_url_from_itemlist(html: str) -> Optional[str]:
    for node in _jsonld_items(html):
        if node.get("@type") in ("ItemList", "CollectionPage"):
            items = node.get("itemListElement") or []
            # ItemList puede tener dicts con {"@type":"ListItem","url":...} o {"item":{"@id"/"url":...}}
            for el in items:
                if isinstance(el, dict):
                    url = el.get("url")
                    if not url and isinstance(el.get("item"), dict):
                        url = el["item"].get("url") or el["item"].get("@id")
                    if url:
                        return url
    return None

def _find_canonical(html: str) -> Optional[str]:
    try:
        soup = BeautifulSoup(html, "html.parser")
        link = soup.find("link", rel=lambda v: v and "canonical" in v)
        if link and link.get("href"):
            return link["href"].strip()
    except Exception:
        pass
    return None

def _detect_platform_from_html(html: str) -> str:
    h = (html or "").lower()
    # señales VTEX
    if "vtex" in h or "/api/catalog_system/" in h or "vtexassets" in h:
        return Platform.VTEX
    # Magento
    if "magento" in h or "mage-init" in h or "data-mage" in h:
        return Platform.MAGENTO
    # PrestaShop
    if "prestashop" in h or "jolisearch" in h:
        return Platform.PRESTASHOP
    # Shopify
    if "cdn.shopify.com" in h or "shopify" in h:
        return Platform.SHOPIFY
    # WooCommerce / WordPress
    if "woocommerce" in h or "wp-content" in h or "wc-add-to-cart" in h:
        return Platform.WOOCOMMERCE
    # Tiendanube
    if "tiendanube" in h or "nuvemshop" in h:
        return Platform.TIENDANUBE
    return Platform.UNKNOWN

@lru_cache(maxsize=256)
def _search_recipes_for(platform: str):
    # Cada receta es una función lambda base, q -> url
    if platform == Platform.VTEX:
        return [
            lambda base, q: f"{base.rstrip('/')}/api/catalog_system/pub/products/search/?fq=alternateIds_Ean:{q}",
            lambda base, q: f"{base.rstrip('/')}/api/catalog_system/pub/products/search/?ft={q}",
            lambda base, q: f"{base.rstrip('/')}/{q}?_q={q}&map=ft",
        ]
    if platform == Platform.MAGENTO:
        return [
            lambda base, q: f"{base.rstrip('/')}/catalogsearch/result/?q={q}",
        ]
    if platform == Platform.PRESTASHOP:
        return [
            lambda base, q: f"{base.rstrip('/')}/module/ambjolisearch/jolisearch?s={q}",
            lambda base, q: f"{base.rstrip('/')}/search?controller=search&s={q}",
            lambda base, q: f"{base.rstrip('/')}/?s={q}",
        ]
    if platform == Platform.SHOPIFY:
        return [
            lambda base, q: f"{base.rstrip('/')}/search?q={q}",
            lambda base, q: f"{base.rstrip('/')}/search/products?q={q}",
        ]
    if platform == Platform.WOOCOMMERCE:
        return [
            lambda base, q: f"{base.rstrip('/')}/?s={q}",
            lambda base, q: f"{base.rstrip('/')}/?post_type=product&s={q}",
        ]
    if platform == Platform.TIENDANUBE:
        return [
            lambda base, q: f"{base.rstrip('/')}/buscar?q={q}",
            lambda base, q: f"{base.rstrip('/')}/search?q={q}",
        ]
    # Genéricas
    return [
        lambda base, q: f"{base.rstrip('/')}/search?q={q}",
        lambda base, q: f"{base.rstrip('/')}/buscar?q={q}",
        lambda base, q: f"{base.rstrip('/')}/busca?q={q}",
        lambda base, q: f"{base.rstrip('/')}/catalogsearch/result/?q={q}",
        lambda base, q: f"{base.rstrip('/')}/?s={q}",
        lambda base, q: f"{base.rstrip('/')}/?q={q}",
    ]

def _first_pdp_from_html_generic(html: str, base: str, platform: str) -> Optional[str]:
    if not html: 
        return None
    basehost = _host(base)

    # 1) Si la página actual ya es PDP (canonical dice PDP), devolvela
    canon = _find_canonical(html)
    if canon:
        u = _abs(base, canon)
        if _host(u).endswith(basehost) and _seems_pdp(u, platform):
            return u

    # 2) JSON-LD ItemList
    u = _first_url_from_itemlist(html)
    if u:
        u = _abs(base, u)
        if _host(u).endswith(basehost) and _seems_pdp(u, platform):
            return u

    # 3) Primer anchor que parezca PDP
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        u = _abs(base, a["href"])
        if not u: 
            continue
        if not _host(u).endswith(basehost):
            continue
        if _seems_pdp(u, platform):
            return u

    return None

# --- Handlers específicos para tiendas no-VTEX ---
async def lookup_generic(client: httpx.AsyncClient, store_name: str, base_url: str, ean: str) -> str:
    """
    Motor genérico:
    - Detecta plataforma con el HTML del home o de la primera búsqueda genérica.
    - Ejecuta recetas de búsqueda para esa plataforma y el EAN.
    - Devuelve la PRIMERA PDP plausible. Si es VTEX y hay API, intenta link desde JSON.
    """
    base = base_url.rstrip("/")
    # 0) Probar detección rápida leyendo el home
    home_html = await fetch_text(client, base)
    platform = _detect_platform_from_html(home_html or "")

    # 1) Si parece VTEX, privilegiar API por EAN/ft (más veloz/preciso)
    if platform == Platform.VTEX:
        api1 = f"{base}/api/catalog_system/pub/products/search/?fq=alternateIds_Ean:{ean}"
        data = await fetch_json(client, api1)
        if isinstance(data, list) and data:
            prod = data[0] or {}
            link = (prod.get("link") or prod.get("linkText") or "").strip()
            if link:
                if not link.startswith("http"):
                    link = f"{base}/{link}"
                    if not link.endswith("/p"): link += "/p"
                return link
        api2 = f"{base}/api/catalog_system/pub/products/search/?ft={ean}"
        data = await fetch_json(client, api2)
        if isinstance(data, list) and data:
            prod = data[0] or {}
            link = (prod.get("link") or prod.get("linkText") or "").strip()
            if link:
                if not link.startswith("http"):
                    link = f"{base}/{link}"
                    if not link.endswith("/p"): link += "/p"
                return link

    # 2) Recetas de búsqueda por plataforma (o genéricas si UNKNOWN)
    recipes = _search_recipes_for(platform)
    for build in recipes:
        url = build(base, ean)
        html = await fetch_text(client, url)
        if not html:
            continue
        u = _first_pdp_from_html_generic(html, base, platform)
        if u:
            # (Opcional) verificación: si el EAN aparece en la PDP, lo preferimos
            try:
                pdp_html = await fetch_text(client, u)
                if pdp_html and ean in pdp_html:
                    return u
            except Exception:
                pass
            # Si no hay verificación, igual devolvemos la primera PDP encontrada
            return u

    return "no encontrado"

async def lookup_meli_robusto(client: httpx.AsyncClient, store_name: str, base_url: str, ean: str) -> str:
    """Mercado Libre: intentar API pública y caer a HTML si hace falta.
    - API (puede requerir token en 2025): /sites/MLA/search?q=<EAN>
    - HTML: listado y rutas alternativas; devolver primer permalink a ítem (/MLA-...) o PDP de catálogo (/p/MLA...)
    """
    api = f"https://api.mercadolibre.com/sites/MLA/search?q={ean}&limit=1"
    try:
        r = await client.get(api, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            j = r.json()
            if isinstance(j, dict):
                results = j.get("results") or []
                if results:
                    link = results[0].get("permalink")
                    if link:
                        return link
    except Exception:
        pass

    bases = [base_url.rstrip("/"), "https://www.mercadolibre.com.ar"]
    paths = [f"/{ean}", f"/jm/search?as_word={ean}", f"/ofertas?query={ean}"]

    def looks_like_meli_item(u: str) -> bool:
        try:
            uu = urlparse(u)
        except Exception:
            return False
        host_ok = "mercadolibre" in uu.netloc
        path = uu.path
        return host_ok and ("/MLA-" in path or "/p/MLA" in path or "/item/" in path or "/up/MLA" in path)

    for b in bases:
        for p in paths:
            url = b + p
            html = await fetch_text(client, url)
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href:
                    continue
                full = href if href.startswith("http") else urljoin("https://www.mercadolibre.com.ar/", href)
                if looks_like_meli_item(full) and not any(x in full for x in ("/login", "/account", "/ayuda", "/help", "/seguridad")):
                    return full
            for tag in soup.select("[data-url], [data-href], [data-link]"):
                val = tag.get("data-url") or tag.get("data-href") or tag.get("data-link")
                if not val:
                    continue
                full = val if val.startswith("http") else urljoin("https://www.mercadolibre.com.ar/", val)
                if looks_like_meli_item(full) and not any(x in full for x in ("/login", "/account", "/ayuda", "/help", "/seguridad")):
                    return full

    return "no encontrado"

async def lookup_central_oeste(client: httpx.AsyncClient, store_name: str, base_url: str, ean: str) -> str:
    """
    Central Oeste (Magento):
    - Buscar en /catalogsearch/result/?q=<EAN>
    - Devolver la PRIMERA PDP del listado.
    - Si la tienda redirige o muestra páginas generales (p.ej. /ofertas.html), devolver "no encontrado".
    """
    base = base_url.rstrip("/")
    search_url = f"{base}/catalogsearch/result/?q={ean}"
    html = await fetch_text(client, search_url)
    if not html:
        return "no encontrado"

    BANNED_LEAFS = {"ofertas.html"}  # 👈 páginas a ignorar
    def same_host(u: str) -> bool:
        try:
            return urlparse(u).netloc.endswith(urlparse(base).netloc)
        except Exception:
            return False

    def abs_url(href: str) -> str:
        return href if href.startswith("http") else urljoin(base + "/", href)

    def looks_like_pdp_url(u: str) -> bool:
        low = u.lower()
        if any(bad in low for bad in ("/login", "/account", "/cart", "/wishlist", "/customer", "/checkout")):
            return False
        try:
            p = urlparse(u).path
        except Exception:
            return False
        leaf = (p.rsplit("/", 1)[-1] or "").lower()
        if leaf in BANNED_LEAFS:              # 👈 ban explícito
            return False
        return p.endswith(".html")            # PDP típica en Magento

    soup = BeautifulSoup(html, "html.parser")

    # 1) Preferencia: enlace típico de PDP en Magento
    first = soup.select_one("a.product-item-link[href]")
    if first:
        u = abs_url(first["href"].strip())
        if same_host(u) and looks_like_pdp_url(u):
            return u

    # 2) Fallback: dentro de ítems de producto
    for tag in soup.select("li.product-item a[href], ol.products a[href]"):
        href = tag.get("href", "").strip()
        if not href or href.startswith("#"):
            continue
        u = abs_url(href)
        if same_host(u) and looks_like_pdp_url(u):
            return u

    # 3) Último recurso: primer anchor que parezca PDP del mismo host
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue
        u = abs_url(href)
        if same_host(u) and looks_like_pdp_url(u):
            return u

    return "no encontrado"

async def lookup_meli(client: httpx.AsyncClient, store_name: str, base_url: str, ean: str) -> str:
    """Mercado Libre: buscar por EAN y devolver el primer item (MLA-...)."""
    search_url = f"{base_url.rstrip('/')}/{ean}"
    html = await fetch_text(client, search_url)
    if not html:
        return "no encontrado"
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        full = href if href.startswith("http") else urljoin(base_url.rstrip("/") + "/", href)
        try:
            uu = urlparse(full)
        except Exception:
            continue
        if "mercadolibre.com" in uu.netloc and "/MLA-" in uu.path:
            return full
    return "no encontrado"

async def lookup_club_beneficios(client: httpx.AsyncClient, store_name: str, base_url: str, ean: str) -> str:
    """Club de Beneficios: usa Magento. Buscamos por EAN y validamos que el PDP contenga el EAN."""
    search_url = f"{base_url.rstrip('/')}/catalogsearch/result/?q={ean}"
    html = await fetch_text(client, search_url)
    candidates = []
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("#"):
                continue
            full = href if href.startswith("http") else urljoin(base_url.rstrip("/") + "/", href)
            try:
                uu = urlparse(full)
            except Exception:
                continue
            if "clubdebeneficios.com" in uu.netloc and uu.path.endswith(".html"):
                leaf = uu.path.split("/")[-1]
                # excluir categorías conocidas
                if leaf in {"productos.html","limpieza.html","almacen.html","bebidas.html","perfumeria.html","ofertas-imperdibles.html"}:
                    continue
                candidates.append(full)
    # Validar que el PDP contenga el EAN en el HTML (SKU/gtin)
    for url in candidates:
        page = await fetch_text(client, url)
        if page and ean in page:
            return url
    return "no encontrado"

async def lookup_atomo(client: httpx.AsyncClient, store_name: str, base_url: str, ean: str) -> str:
    """
    Átomo (PrestaShop + JoliSearch):
    - Busca con /module/ambjolisearch/jolisearch?s=<EAN> y fallbacks.
    - Devuelve SIEMPRE la PRIMERA PDP del listado (si existe).
    - No valida EAN; objetivo: primer resultado real.
    - Si no hay ningún resultado real -> "no encontrado".
    """
    base = base_url.rstrip("/")
    search_urls = [
        f"{base}/module/ambjolisearch/jolisearch?s={ean}",   # buscador real JoliSearch
        f"{base}/search?controller=search&s={ean}",          # fallback PrestaShop
        f"{base}/?s={ean}",                                  # ultra fallback
    ]

    banned = (
        "/cart", "/login", "/my-account", "/account", "/checkout", "/wishlist",
        "/module/ambjolisearch/jolisearch"  # no devolver la propia página de búsqueda
    )
    try:
        base_host = urlparse(base).netloc
    except Exception:
        base_host = ""

    def normalize(href: str) -> str:
        if not href:
            return ""
        href = href.strip()
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("http"):
            return href
        return urljoin(base + "/", href.lstrip("/"))

    def first_pdp_from_html(html: str) -> Optional[str]:
        if not html:
            return None
        # Limitar el tamaño a escanear para evitar costos locos en HTML gigantes
        html = html[:600_000]
        # Buscar el PRIMER href que:
        # - termine en .html
        # - sea mismo host
        # - no tenga rutas baneadas
        # - tenga un ID numérico típico de PrestaShop en el path (p.ej. /36315-...)
        for m in re.finditer(r'href=["\']([^"\']+?\.html)(?:\?[^"\']*)?["\']', html, re.IGNORECASE):
            full = normalize(m.group(1))
            if not full:
                continue
            low = full.lower()
            if any(x in low for x in banned):
                continue
            try:
                uu = urlparse(full)
            except Exception:
                continue
            if base_host and base_host not in uu.netloc:
                continue
            path = uu.path.lower()
            if re.search(r"/\d{3,}[-/]", path) or re.search(r"[-/]\d{3,}\.html$", path):
                return full
        return None

    for su in search_urls:
        html = await fetch_text(client, su)
        if not html:
            continue

        # Si justo aterrizamos en una PDP (por redirección), probá canonical primero
        try:
            soup = BeautifulSoup(html, "html.parser")
            canon = soup.find("link", rel="canonical")
            if canon and canon.get("href"):
                u = normalize(canon["href"])
                # Reutilizamos el mismo criterio del parser para validar rápidamente
                maybe = first_pdp_from_html(f'<a href="{u}">x</a>')
                if maybe:
                    return maybe
        except Exception:
            pass

        # Extraer el PRIMER PDP del listado por regex (no dependemos de clases CSS)
        first = first_pdp_from_html(html)
        if first:
            return first

    return "no encontrado"

# Registrar handlers por tienda (por defecto: VTEX -> lookup_in_store)
LOOKUP_HANDLERS = {
    "mercado_libre": lookup_meli_robusto,
    "club_de_beneficios": lookup_club_beneficios,
    "central_oeste": lookup_central_oeste,
    "atomo": lookup_atomo,
}


async def process_eans(eans: List[str], progress_cb=None, stores_map: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    rows = []
    sem = asyncio.Semaphore(MAX_PARALLEL_PER_EAN)
    total = len(eans)
    done = 0
    stores = stores_map or STORES  # 👈 usar las elegidas o todas

    async with httpx.AsyncClient(follow_redirects=True, headers=DEFAULT_HEADERS) as client:
        for raw in eans:
            ean = sanitize_ean(raw)
            if not ean:
                row = {"EAN": str(raw)}
                for name in stores.keys():
                    row[name] = "no encontrado"
                rows.append(row)
                done += 1
                if progress_cb: progress_cb(done, total)
                continue

            async def task_for(store_name: str, base_url: str) -> str:
                async with sem:
                    handler = LOOKUP_HANDLERS.get(store_name, lookup_generic)
                    try:
                        return await asyncio.wait_for(
                            handler(client, store_name, base_url, ean),
                            timeout=PER_STORE_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        return "no encontrado"
                    except Exception:
                        return "no encontrado"

            tasks = [task_for(n, u) for n, u in stores.items()]
            results = await asyncio.gather(*tasks)
            row = {"EAN": ean}
            for (name, _), url in zip(stores.items(), results):
                row[name] = url
            rows.append(row)

            done += 1
            if progress_cb: progress_cb(done, total)

    return pd.DataFrame(rows)

async def run_job(job_id: str, eans: List[str], stores_map: Optional[Dict[str, str]] = None) -> None:
    def _cb(done: int, total: int) -> None:
        PROGRESS[job_id] = {"done": done, "total": total, "status": "running"}

    df = await process_eans(eans, progress_cb=_cb, stores_map=stores_map)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Resultados")
    buf.seek(0)

    RESULTS[job_id] = buf
    PROGRESS[job_id] = {"done": len(eans), "total": len(eans), "status": "finished"}

# --------------------------- FastAPI ---------------------------
app = FastAPI(title="Buscador de URLs por EAN", version="1.0.0")

@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    stores: Optional[List[str]] = Form(None),
    custom_slug: Optional[List[str]] = Form(None),
    custom_name: Optional[List[str]] = Form(None),  # no se usa en backend, pero lo recibimos
    custom_url: Optional[List[str]] = Form(None),
):
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Subí un .xlsx (Excel moderno)")

    # --- leer excel
    raw = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(raw), dtype=str)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No pude leer el Excel: {e}")
    if df.empty:
        raise HTTPException(status_code=400, detail="El Excel está vacío")

    # --- detectar columna EAN
    cols_norm = {c.strip(): c for c in df.columns}
    target_col = next((cols_norm[c] for c in cols_norm if c.lower() == "ean"), None)
    if target_col is None:
        raise HTTPException(status_code=400, detail="No encontré una columna llamada 'EAN'")

    eans = [sanitize_ean(v) for v in df[target_col].tolist()]

    # --- armar mapa de tiendas manuales
    def _norm_url(u: str) -> str:
        if not u:
            return ""
        u = u.strip()
        if not u.startswith("http://") and not u.startswith("https://"):
            u = "https://" + u.lstrip("/")
        return u.rstrip("/")

    custom_map: Dict[str, str] = {}
    if custom_slug and custom_url:
        for s, u in zip(custom_slug, custom_url):
            s = (s or "").strip()
            u = _norm_url(u or "")
            if s and u:
                custom_map[s] = u

    # universo = predefinidas + manuales
    all_stores = dict(STORES)
    all_stores.update(custom_map)

    # selección desde checkboxes; si no vino nada, usar todas por defecto (incluye manuales)
    if stores:
        selected = {k: v for k, v in all_stores.items() if k in set(stores)}
        if not selected:
            selected = all_stores
    else:
        selected = all_stores

    # --- procesar y devolver excel
    out_df = await process_eans(eans, stores_map=selected)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        out_df.to_excel(writer, index=False, sheet_name="Resultados")
    buf.seek(0)

    filename = re.sub(r"[^\w\-]+", "_", file.filename.rsplit(".", 1)[0]) + "_resultados.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Cache-Control": "no-store",
        },
    )

@app.post("/start")
async def start(
    file: UploadFile = File(...),
    stores: Optional[List[str]] = Form(None),
    custom_slug: Optional[List[str]] = Form(None),
    custom_name: Optional[List[str]] = Form(None),  # no se usa en backend, pero lo recibimos
    custom_url: Optional[List[str]] = Form(None),
):
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Subí un .xlsx (Excel moderno)")

    # --- leer excel
    raw = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(raw), dtype=str)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No pude leer el Excel: {e}")
    if df.empty:
        raise HTTPException(status_code=400, detail="El Excel está vacío")

    # --- detectar columna EAN
    cols_norm = {c.strip(): c for c in df.columns}
    target_col = next((cols_norm[c] for c in cols_norm if c.lower() == "ean"), None)
    if target_col is None:
        raise HTTPException(status_code=400, detail="No encontré una columna llamada 'EAN'")

    eans = [sanitize_ean(v) for v in df[target_col].tolist()]

    # --- armar mapa de tiendas manuales
    def _norm_url(u: str) -> str:
        if not u:
            return ""
        u = u.strip()
        if not u.startswith("http://") and not u.startswith("https://"):
            u = "https://" + u.lstrip("/")
        return u.rstrip("/")

    custom_map: Dict[str, str] = {}
    if custom_slug and custom_url:
        for s, u in zip(custom_slug, custom_url):
            s = (s or "").strip()
            u = _norm_url(u or "")
            if s and u:
                custom_map[s] = u

    # universo = predefinidas + manuales
    all_stores = dict(STORES)
    all_stores.update(custom_map)

    # selección desde checkboxes; si no vino nada, usar todas por defecto (incluye manuales)
    if stores:
        selected = {k: v for k, v in all_stores.items() if k in set(stores)}
        if not selected:
            selected = all_stores
    else:
        selected = all_stores

    # --- lanzar job asíncrono
    job_id = uuid4().hex
    PROGRESS[job_id] = {"done": 0, "total": len(eans), "status": "running"}
    asyncio.create_task(run_job(job_id, eans, stores_map=selected))
    return {"job_id": job_id, "total": len(eans)}

@app.get("/progress/{job_id}")
async def progress(job_id: str):
    return PROGRESS.get(job_id, {"done": 0, "total": 1, "status": "unknown"})

@app.get("/download/{job_id}")
async def download(job_id: str):
    buf = RESULTS.get(job_id)
    if not buf:
        raise HTTPException(status_code=404, detail="Aún no está listo o job inexistente")
    return StreamingResponse(
        io.BytesIO(buf.getvalue()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=resultados_{job_id}.xlsx",
            "Cache-Control": "no-store",
        },
    )

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    # construir lista de checkboxes con las tiendas disponibles
    items = []
    for key, url in STORES.items():
        label = FRIENDLY_NAMES.get(key, key.replace("_", " ").title())
        items.append(f'''
          <label class="store-opt">
            <input type="checkbox" name="stores" value="{key}" checked />
            <span>{label}</span>
          </label>
        ''')
    stores_checkboxes = "\n".join(items)

    return """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Buscador de URLs por EAN</title>
  <style>
    :root { --bg:#0b0c10; --card:#111318; --accent:#7c5cff; --text:#e7e9ee; --muted:#aab0bc; }
    * { box-sizing:border-box; }
    body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, "Helvetica Neue", Arial; background: var(--bg); color: var(--text); }
    header { position:sticky; top:0; background:rgba(11,12,16,.8); backdrop-filter: blur(6px); border-bottom:1px solid rgba(255,255,255,.08); }
    .nav { max-width: 1000px; margin:0 auto; display:flex; align-items:center; justify-content:space-between; padding:12px 16px; }
    .nav a { color: var(--text); text-decoration:none; opacity:.9; }
    .nav a:hover { opacity:1; }
    .wrap { min-height:100dvh; display:grid; place-items:center; padding: 24px; }
    .card { width: 100%; max-width: 980px; background: linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.02)); border: 1px solid rgba(255,255,255,.08); border-radius: 20px; padding: 28px; box-shadow: 0 10px 30px rgba(0,0,0,.35); }
    h1 { margin:0 0 8px; font-size: 26px; letter-spacing: .3px; }
    p.lead { margin:0 0 18px; color: var(--muted); }
    .upload { display:flex; flex-direction:column; gap:16px; margin-top: 14px; }
    label { font-size:14px; color: var(--muted); }
    input[type=file] { padding: 18px; border-radius: 14px; border: 1px dashed rgba(255,255,255,.18); background: rgba(255,255,255,.02); color: var(--text); }
    .btn { display:inline-flex; align-items:center; gap:10px; padding: 12px 18px; background: var(--accent); color:#fff; border:0; border-radius: 12px; font-weight:600; cursor:pointer; }
    .btn[disabled] { opacity:.7; cursor:not-allowed; }
    .btn:hover { filter: brightness(1.05); }
    .foot { font-size: 12px; color: var(--muted); }
    .bar { width:100%; height:14px; border-radius: 10px; }

    .stores-box { border:1px solid rgba(255,255,255,.12); border-radius:16px; padding:12px; }
    .stores-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:8px; }
    .stores-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap:10px; }
    .store-opt { display:flex; align-items:center; gap:10px; padding:8px 10px; border-radius:10px; }
    .store-opt:hover { background:rgba(255,255,255,.04); }
    .tiny { font-size:12px; color:var(--muted); }

    .manual { border:1px dashed rgba(255,255,255,.18); border-radius:14px; padding:12px; margin-top:10px; }
    .row { display:flex; gap:10px; flex-wrap:wrap; }
    .row input[type=text] { flex:1; min-width:200px; padding:10px 12px; border-radius:10px; border:1px solid rgba(255,255,255,.12); background: rgba(255,255,255,.02); color: var(--text); }
    .chip { display:inline-flex; align-items:center; gap:8px; padding:6px 10px; border-radius:999px; background: rgba(255,255,255,.08); margin-top:8px; }
    .chip b { font-weight:600; }
    .chip button { background:none; border:0; color:#fff; cursor:pointer; }
  </style>
</head>
<body>
  <header>
    <div class="nav">
      <strong>Buscador de URLs por EAN</strong>
      <nav style="display:flex; gap:14px;">
        <a href="/info">¿Cómo funciona?</a>
        <a href="/health">Health</a>
      </nav>
    </div>
  </header>
  <div class="wrap">
    <div class="card">
      <h1>🔎 Procesar Excel con EAN</h1>
      <p class="lead">Subí un <strong>Excel (.xlsx)</strong> con una columna llamada <code>EAN</code>. Elegí en qué tiendas buscar (incluye tiendas manuales) y descargá el Excel con resultados.</p>

      <form class="upload" id="form" enctype="multipart/form-data">
        <label for="file">Elegí tu archivo (.xlsx)</label>
        <input id="file" name="file" type="file" accept=".xlsx" required />

        <div class="stores-box">
          <div class="stores-head">
            <strong>Tiendas a consultar</strong>
            <div>
              <button type="button" class="btn tiny" id="sel-all">Seleccionar todas</button>
              <button type="button" class="btn tiny" id="sel-none" style="margin-left:8px;">Vaciar selección</button>
            </div>
          </div>
          <div class="stores-grid" id="stores-grid">
            {stores_checkboxes}
          </div>
          <div class="tiny" style="margin-top:6px;">Podés elegir una, varias o todas.</div>

          <div class="manual">
            <div class="tiny" style="margin-bottom:6px;"><strong>Agregar tienda manual</strong> (para cualquier sitio web):</div>
            <div class="row">
              <input type="text" id="m-name" placeholder="Nombre (ej. Mi Super)" />
              <input type="text" id="m-url" placeholder="URL base (ej. https://mi-super.com)" />
              <button type="button" class="btn" id="m-add">Agregar tienda</button>
            </div>
            <div id="m-list"></div>
            <!-- Contenedor donde agrego inputs ocultos con las tiendas manuales -->
            <div id="m-hidden"></div>
          </div>
        </div>

        <button class="btn" type="submit" id="btn">
          <span id="btn-text">Procesar y descargar Excel</span>
        </button>
        <progress id="prog" class="bar" value="0" max="100"></progress>
        <div id="status" class="foot"></div>
      </form>

      <div class="foot" style="margin-top:12px;">Tip: para tiendas manuales no hace falta programar nada; el motor detecta la plataforma y busca el primer producto.</div>
    </div>
  </div>
<script>
const form = document.getElementById('form');
const btn = document.getElementById('btn');
const btnText = document.getElementById('btn-text');
const bar = document.getElementById('prog');
const statusEl = document.getElementById('status');
const grid = document.getElementById('stores-grid');
const mName = document.getElementById('m-name');
const mUrl  = document.getElementById('m-url');
const mAdd  = document.getElementById('m-add');
const mList = document.getElementById('m-list');
const mHidden = document.getElementById('m-hidden');

document.getElementById('sel-all').addEventListener('click', () => {
  document.querySelectorAll('input[name="stores"]').forEach(cb => cb.checked = true);
});
document.getElementById('sel-none').addEventListener('click', () => {
  document.querySelectorAll('input[name="stores"]').forEach(cb => cb.checked = false);
});

function slugify(s) {
  return (s || '')
    .toLowerCase()
    .normalize('NFD').replace(/[\u0300-\u036f]/g,'')
    .replace(/[^a-z0-9]+/g,'_')
    .replace(/^_+|_+$/g,'')
    .substring(0, 40) || 'tienda';
}

function ensureUniqueSlug(base) {
  let s = base, i = 1;
  const existing = new Set(Array.from(document.querySelectorAll('input[name="stores"]')).map(x => x.value));
  while (existing.has(s)) { s = base + '_' + (++i); }
  return s;
}

function normalizeUrl(u) {
  u = (u || '').trim();
  if (!u) return '';
  if (!/^https?:\/\//i.test(u)) u = 'https://' + u.replace(/^\/+/, '');
  return u.replace(/\/+$/,'');
}

function addManualStore(name, url) {
  const nice = name && name.trim() ? name.trim() : (new URL(url)).hostname.replace(/^www\./,'');
  const slug = ensureUniqueSlug(slugify(nice));
  // 1) Checkbox visible
  const label = document.createElement('label');
  label.className = 'store-opt';
  label.innerHTML = `
    <input type="checkbox" name="stores" value="${slug}" checked />
    <span>${nice}</span>
  `;
  grid.appendChild(label);
  // 2) Chip con botón de borrar
  const chip = document.createElement('span');
  chip.className = 'chip';
  chip.dataset.slug = slug;
  chip.innerHTML = `<b>${nice}</b> · ${url} <button type="button" aria-label="Quitar">✕</button>`;
  chip.querySelector('button').addEventListener('click', () => removeManualStore(slug, chip));
  mList.appendChild(chip);
  // 3) Inputs ocultos (slug, name, url) para enviar al backend
  const h1 = document.createElement('input'); h1.type = 'hidden'; h1.name = 'custom_slug'; h1.value = slug;
  const h2 = document.createElement('input'); h2.type = 'hidden'; h2.name = 'custom_name'; h2.value = nice;
  const h3 = document.createElement('input'); h3.type = 'hidden'; h3.name = 'custom_url';  h3.value = url;
  h1.id = 'h_slug_'+slug; h2.id = 'h_name_'+slug; h3.id = 'h_url_'+slug;
  mHidden.appendChild(h1); mHidden.appendChild(h2); mHidden.appendChild(h3);
}

function removeManualStore(slug, chip) {
  // borrar chip
  if (chip && chip.parentNode) chip.parentNode.removeChild(chip);
  // borrar checkbox
  const cb = Array.from(document.querySelectorAll('input[name="stores"]')).find(x => x.value === slug);
  if (cb && cb.parentNode) cb.parentNode.parentNode.removeChild(cb.parentNode);
  // borrar ocultos
  ['slug','name','url'].forEach(k => {
    const el = document.getElementById('h_'+k+'_'+slug);
    if (el && el.parentNode) el.parentNode.removeChild(el);
  });
}

mAdd.addEventListener('click', () => {
  const url = normalizeUrl(mUrl.value);
  if (!url) { alert('Pegá la URL base de la tienda'); return; }
  try { new URL(url); } catch { alert('URL inválida'); return; }
  const name = mName.value || '';
  addManualStore(name, url);
  mName.value = ''; mUrl.value = '';
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!document.getElementById('file').files.length) return;
  toggle(true);
  const fd = new FormData(form);
  try {
    const start = await fetch('/start', { method: 'POST', body: fd });
    if (!start.ok) {
      const txt = await start.text();
      throw new Error(txt || 'Error iniciando el procesamiento');
    }
    const data = await start.json();
    await poll(data.job_id);
  } catch (err) {
    alert(err.message || 'Error inesperado');
    toggle(false);
  }
});

function toggle(loading) {
  if (loading) {
    btn.setAttribute('disabled','true');
    btnText.textContent = 'Procesando…';
    bar.value = 0;
    statusEl.textContent = '';
  } else {
    btn.removeAttribute('disabled');
    btnText.textContent = 'Procesar y descargar Excel';
  }
}

async function poll(job) {
  let finished = false;
  while (!finished) {
    await new Promise(r => setTimeout(r, 600));
    const r = await fetch(`/progress/${job}`);
    if (!r.ok) continue;
    const p = await r.json();
    const done = p.done || 0;
    const total = p.total || 1;
    const percent = Math.floor((done / total) * 100);
    bar.value = percent;
    statusEl.textContent = `Procesado ${done}/${total} (${percent}%)`;
    finished = p.status === 'finished' || done >= total;
  }
  statusEl.textContent = 'Listo. Descargando…';
  window.location.href = `/download/${job}`;
  toggle(false);
}
</script>
</body>
</html>
    """.replace("{stores_checkboxes}", stores_checkboxes)

@app.get("/info", response_class=HTMLResponse)
async def info() -> str:
    # Nombres "lindos" para mostrar
    friendly_names = {
        "carrefour": "Carrefour",
        "jumbo": "Jumbo",
        "disco": "Disco",
        "vea": "Vea",
        "dia": "Día",
        "farmacity": "Farmacity",
        "mas_online": "Más Online",
        "pigmento": "Perfumerías Pigmento",
        "mercado_libre": "Mercado Libre",
        "club_de_beneficios": "Club de Beneficios",
        "central_oeste": "Central Oeste",
        "atomo": "Átomo",
    }

    # Armar tarjetas de tiendas con favicon como logo
    store_cards = []
    for key, url in STORES.items():
        name = friendly_names.get(key, key.replace("_", " ").title())
        try:
            netloc = urlparse(url).netloc or urlparse("https://" + urlparse(url).path).netloc
        except Exception:
            netloc = ""
        # Servicio de favicons de Google: rápido y suele traer el logo del sitio
        logo = f"https://www.google.com/s2/favicons?domain={netloc}&sz=128"
        card = f"""
        <a class="store" href="{url}" target="_blank" rel="noopener">
          <div class="logo-wrap"><img src="{logo}" alt="Logo {name}" loading="lazy" /></div>
          <div class="store-name">{name}</div>
          <div class="store-link">Abrir sitio ↗</div>
        </a>
        """
        store_cards.append(card)

    stores_html = "\n".join(store_cards)

    # Página completa (texto explicativo + sección de tiendas)
    return """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>¿Cómo funciona? — Buscador de URLs por EAN</title>
  <style>
    :root { --bg:#0b0c10; --card:#111318; --accent:#7c5cff; --text:#e7e9ee; --muted:#aab0bc; }
    * { box-sizing:border-box; }
    body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, "Helvetica Neue", Arial; background: var(--bg); color: var(--text); }
    .shell { max-width: 1000px; margin: 0 auto; padding: 28px 18px 80px; }
    .card { background: linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.02)); border: 1px solid rgba(255,255,255,.08); border-radius: 20px; padding: 22px 24px; margin-bottom: 16px; }
    h1 { margin: 12px 0 10px; font-size: 28px; }
    h2 { margin: 18px 0 8px; font-size: 20px; }
    h3 { margin: 14px 0 8px; font-size: 16px; color: var(--text); }
    p, li { color: var(--muted); line-height: 1.6; }
    code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; }
    .cta { display:flex; gap:12px; margin: 18px 0 28px; }
    .btn { display:inline-flex; align-items:center; gap:10px; padding: 12px 18px; background: var(--accent); color:#fff; border:0; border-radius: 12px; font-weight:600; cursor:pointer; text-decoration:none; }
    .btn.secondary { background: transparent; border:1px solid rgba(255,255,255,.22); color: var(--text); }
    .list { margin-left: 20px; }
    .note { font-size: 13px; color: var(--muted); opacity: .9; }

    /* Sección tiendas */
    #stores .stores { display:grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap:14px; margin-top: 14px; }
    #stores .store { display:flex; flex-direction:column; align-items:center; gap:8px; padding:16px; border-radius:16px; text-decoration:none; color:var(--text);
                      border:1px solid rgba(255,255,255,.08); background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.02)); }
    #stores .store:hover { border-color: rgba(255,255,255,.18); transform: translateY(-1px); transition: .15s ease; }
    .logo-wrap { width:56px; height:56px; border-radius:14px; background: rgba(255,255,255,.06); display:grid; place-items:center; }
    .logo-wrap img { width:32px; height:32px; }
    .store-name { font-weight:600; text-align:center; }
    .store-link { font-size:12px; color: var(--muted); }
  </style>
</head>
<body>
  <div class="shell">
    <div style="display:flex; justify-content:space-between; align-items:center; gap:12px;">
      <h1>¿Cómo funciona la app?</h1>
      <div class="cta">
        <a class="btn" href="/">▶️ Probar ahora</a>
        <a class="btn secondary" href="#stores">Ver tiendas disponibles</a>
      </div>
    </div>

    <div class="card">
      <h2>Qué hace, en pocas palabras</h2>
      <p>
        Esta herramienta toma un archivo de Excel con una columna llamada <strong>EAN</strong> (el código de barras de cada producto) y
        busca en varias tiendas online el <strong>primer resultado de producto</strong> que encuentra para cada código.
        Al final te devuelve <strong>otro Excel</strong> con una columna por tienda y el <strong>link directo</strong> a la página del producto.
        Si en una tienda el producto no aparece, verás <em>“no encontrado”</em>.
      </p>
      <p>Mientras trabaja, verás una <strong>barra de progreso</strong> para seguir el avance.</p>
    </div>

    <div class="card">
      <h2>Guía paso a paso (para quienes no programan)</h2>
      <ol class="list">
        <li><strong>Prepará tu Excel:</strong> debe ser <span class="mono">.xlsx</span> y tener una columna llamada exactamente <strong>EAN</strong>. Cada fila es un producto. No hace falta ninguna otra columna.</li>
        <li><strong>Abrí la app:</strong> desde la página principal apretá <em>Elegir archivo</em> y seleccioná tu Excel.</li>
        <li><strong>Iniciá el proceso:</strong> hacé clic en <em>Procesar y descargar Excel</em>. La app empieza a buscar en las tiendas.</li>
        <li><strong>Seguí el progreso:</strong> la barra muestra el % completado y cuántos códigos ya se procesaron.</li>
        <li><strong>Descarga automática:</strong> cuando termina, tu navegador descarga un Excel con los resultados.</li>
        <li><strong>Leé los resultados:</strong> cada fila conserva el EAN y se agregan columnas por tienda. En cada columna ves el <strong>link</strong> al producto o <em>“no encontrado”</em>.</li>
      </ol>
      <p class="note">Tip: si tu Excel muestra números como <code>7793742007897.0</code>, no te preocupes. La app limpia puntos y conserva ceros a la izquierda automáticamente.</p>
    </div>

    <div class="card">
      <h2>Qué hace “por dentro” (explicado simple)</h2>
      <div>
        <h3>1) Limpia tus EAN</h3>
        <p>La app estandariza cada código: quita espacios, puntos o guiones y se queda con los dígitos. Así evita errores.</p>
        <h3>2) Pregunta a cada tienda</h3>
        <p>Para sitios con plataforma <strong>VTEX</strong> (Carrefour, Jumbo, Disco, Vea, Día, Farmacity, Más Online, Pigmento), primero usa una <strong>API</strong> y, si no hay match, explora la página de resultados y toma la <strong>primera PDP</strong> válida.</p>
        <h3>3) Otras plataformas</h3>
        <p>Para Magento/PrestaShop, abre la búsqueda pública y toma la <strong>primera página de producto</strong>, evitando páginas como “ofertas”, “login”, “carrito”.</p>
      </div>
    </div>

    <div class="card">
      <h2>Cómo interpretar el Excel de salida</h2>
      <ul class="list">
        <li><strong>URL:</strong> link directo a la página del producto (PDP) en esa tienda.</li>
        <li><strong>“no encontrado”:</strong> la tienda no mostró resultados o respondió lento. Podés reintentar más tarde.</li>
        <li><strong>Coincidencias aproximadas:</strong> algunas tiendas devuelven resultados similares. La app toma <em>el primer resultado</em>; verificalo si es crítico.</li>
      </ul>
    </div>

    <div class="card">
      <h2>Tiendas disponibles</h2>
      <p>La lista se genera automáticamente a partir de la configuración actual de la app.</p>
      <div id="stores" class="stores">
""" + stores_html + """
      </div>
    </div>

    <div class="card">
      <h2>Sección para desarrolladores</h2>
      <h3>Arquitectura</h3>
      <ul class="list">
        <li><strong>Backend:</strong> FastAPI + Uvicorn (Python asíncrono).</li>
        <li><strong>HTTP client:</strong> httpx.AsyncClient con follow_redirects=True.</li>
        <li><strong>Paralelismo:</strong> semáforo por EAN (MAX_PARALLEL_PER_EAN) y asyncio.gather.</li>
        <li><strong>Timeouts:</strong> REQUEST_TIMEOUT por request y PER_STORE_TIMEOUT por tienda.</li>
        <li><strong>Estado en memoria:</strong> PROGRESS y RESULTS guardan el avance y el Excel por job.</li>
      </ul>

      <h3>Flujo</h3>
      <ol class="list">
        <li>POST /start recibe el Excel, detecta la columna EAN, crea un job_id y lanza run_job con asyncio.create_task.</li>
        <li>run_job → process_eans: por cada EAN crea tareas por tienda (handler por tienda).</li>
        <li>VTEX: API por EAN (fq=alternateIds_Ean), luego API por texto (ft=), luego HTML (PDP que termina en /p).</li>
        <li>No-VTEX: búsqueda pública (Magento catalogsearch, PrestaShop/JoliSearch), se toma la primera PDP evitando login/cart/ofertas.</li>
        <li>Al final, Excel en memoria y descarga por GET /download/{job_id}.</li>
      </ol>

      <h3>Endpoints</h3>
      <ul class="list">
        <li>GET / — formulario y barra de progreso.</li>
        <li>POST /start — crea job y arranca el procesamiento.</li>
        <li>GET /progress/{job_id} — avance {done,total,status}.</li>
        <li>GET /download/{job_id} — descarga Excel final.</li>
        <li>POST /upload — modo síncrono (devuelve Excel en la misma llamada).</li>
        <li>GET /health — JSON con tiendas (para debug/monitor).</li>
        <li>GET /info — esta página.</li>
      </ul>
    </div>

    <div class="cta">
      <a class="btn" href="/">▶️ Probar ahora</a>
    </div>
  </div>
</body>
</html>
    """

# --------------------------- Healthcheck sencillo ---------------------------
@app.get("/health")
async def health():
    return {"ok": True, "stores": list(STORES.keys())}
