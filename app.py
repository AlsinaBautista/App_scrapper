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

import asyncio
import io
import re
from typing import Dict, List, Optional

from urllib.parse import urljoin, urlparse

import httpx
import pandas as pd
from bs4 import BeautifulSoup
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse

PROGRESS = {}     # job_id -> {"done": int, "total": int, "status": "running"/"finished"}
RESULTS = {}      # job_id -> BytesIO con el Excel


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
    "pigmento": "https://www.pigmento.com.ar",
    "coto": "https://www.cotodigital3.com.ar",
    "mercado_libre": "https://listado.mercadolibre.com.ar",
    "club_de_beneficios": "https://clubdebeneficios.com",
}

# Concurrencia (ajustá si necesitás ser más/menos agresivo)
MAX_PARALLEL_PER_EAN = 6
REQUEST_TIMEOUT = 12.0

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
)

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
            # En VTEX aceptamos PDP aunque no termine en /p si vino como link absoluto
            return link
        lt = prod.get("linkText")
        if lt:
            return f"{base_url.rstrip('/')}/{lt}/p"

    # 2) API full-text (algunas tiendas indexan el EAN sólo por ft)
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

    # 2) Página de búsqueda genérica
    search_url = build_search_page_url(base_url, ean)
    html = await fetch_text(client, search_url)
    if html:
        maybe = first_product_link_from_html(html, base_url)
        if maybe and is_valid_pdp(maybe, base_url):
            return maybe

    return "no encontrado"

# --- Handlers específicos para tiendas no-VTEX ---
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

async def lookup_coto(client: httpx.AsyncClient, store_name: str, base_url: str, ean: str) -> str:
    """Coto: probar varias URLs de búsqueda y tomar primer producto (ruta con 'producto'/'productos'/'product' o '/p/')."""
    candidates = [
        f"{base_url.rstrip('/')}/sitios/cd3/busca?keyword={ean}",
        f"{base_url.rstrip('/')}/busca?keyword={ean}",
        f"{base_url.rstrip('/')}/buscar?q={ean}",
        f"{base_url.rstrip('/')}/buscar?texto={ean}",
        f"{base_url.rstrip('/')}/s?q={ean}",
    ]
    for url in candidates:
        html = await fetch_text(client, url)
        if not html:
            continue
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
            if ("coto" in uu.netloc) and ("/producto" in uu.path or "/productos" in uu.path or "/product" in uu.path or "/p/" in uu.path):
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

# Registrar handlers por tienda (por defecto: VTEX -> lookup_in_store)
LOOKUP_HANDLERS = {
    "coto": lookup_coto,
    "mercado_libre": lookup_meli,
    "club_de_beneficios": lookup_club_beneficios,
}


async def process_eans(eans: List[str], progress_cb=None) -> pd.DataFrame:
    rows = []
    sem = asyncio.Semaphore(MAX_PARALLEL_PER_EAN)
    total = len(eans)
    done = 0

    async with httpx.AsyncClient(follow_redirects=True, headers=DEFAULT_HEADERS) as client:
        for raw in eans:
            ean = sanitize_ean(raw)
            if not ean:
                row = {"EAN": str(raw)}
                for name in STORES.keys():
                    row[name] = "no encontrado"
                rows.append(row)
                done += 1
                if progress_cb: progress_cb(done, total)     # 👈
                continue

            async def task_for(store_name: str, base_url: str) -> str:
                async with sem:
                    handler = LOOKUP_HANDLERS.get(store_name, lookup_in_store)
                    return await lookup_in_store(client, store_name, base_url, ean)

            tasks = [task_for(n, u) for n, u in STORES.items()]
            results = await asyncio.gather(*tasks)
            row = {"EAN": ean}
            for (name, _), url in zip(STORES.items(), results):
                row[name] = url
            rows.append(row)

            done += 1
            if progress_cb: progress_cb(done, total)         # 👈

    return pd.DataFrame(rows)

async def run_job(job_id: str, eans: List[str]) -> None:
    def _cb(done: int, total: int) -> None:
        PROGRESS[job_id] = {"done": done, "total": total, "status": "running"}

    df = await process_eans(eans, progress_cb=_cb)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Resultados")
    buf.seek(0)

    RESULTS[job_id] = buf
    PROGRESS[job_id] = {"done": len(eans), "total": len(eans), "status": "finished"}


# --------------------------- FastAPI ---------------------------
app = FastAPI(title="Buscador de URLs por EAN", version="1.0.0")
@app.post("/start")
async def start(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Subí un .xlsx (Excel moderno)")

    raw = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(raw), dtype=str)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No pude leer el Excel: {e}")
    if df.empty:
        raise HTTPException(status_code=400, detail="El Excel está vacío")

    # columna EAN (case-insensitive)
    cols_norm = {c.strip(): c for c in df.columns}
    target_col = next((cols_norm[c] for c in cols_norm if c.lower() == "ean"), None)
    if target_col is None:
        raise HTTPException(status_code=400, detail="No encontré una columna llamada 'EAN'")

    eans = [sanitize_ean(v) for v in df[target_col].tolist()]

    job_id = uuid4().hex
    PROGRESS[job_id] = {"done": 0, "total": len(eans), "status": "running"}
    asyncio.create_task(run_job(job_id, eans))
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
    .wrap { min-height:100dvh; display:grid; place-items:center; padding: 24px; }
    .card { width: 100%; max-width: 760px; background: linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.02)); border: 1px solid rgba(255,255,255,.08); border-radius: 20px; padding: 28px; box-shadow: 0 10px 30px rgba(0,0,0,.35); }
    h1 { margin:0 0 8px; font-size: 26px; letter-spacing: .3px; }
    p.lead { margin:0 0 18px; color: var(--muted); }
    ul { margin: 8px 0 20px 22px; color: var(--muted); }
    .upload { display:flex; flex-direction:column; gap:16px; margin-top: 14px; }
    label { font-size:14px; color: var(--muted); }
    input[type=file] { padding: 18px; border-radius: 14px; border: 1px dashed rgba(255,255,255,.18); background: rgba(255,255,255,.02); color: var(--text); }
    .btn { display:inline-flex; align-items:center; gap:10px; padding: 12px 18px; background: var(--accent); color:#fff; border:0; border-radius: 12px; font-weight:600; cursor:pointer; }
    .btn[disabled] { opacity:.7; cursor:not-allowed; }
    .btn:hover { filter: brightness(1.05); }
    .foot { font-size: 12px; color: var(--muted); }
    .bar { width:100%; height:14px; border-radius: 10px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>🔎 Buscador de URLs por EAN</h1>
      <p class="lead">Subí un <strong>Excel (.xlsx)</strong> con una columna llamada <code class="k">EAN</code>. Te devuelvo otro Excel con el primer resultado por supermercado.</p>
      <ul>
        <li>Soporta: Carrefour, Jumbo, Disco, Vea y Día.</li>
        <li>Si no se encuentra el producto en un sitio, verás <em>"no encontrado"</em>.</li>
        <li>Los códigos se tratan como texto para preservar ceros a la izquierda.</li>
      </ul>
      <form class="upload" id="form" enctype="multipart/form-data">
        <label for="file">Elegí tu archivo (.xlsx)</label>
        <input id="file" name="file" type="file" accept=".xlsx" required />
        <button class="btn" type="submit" id="btn">
          <span id="btn-text">Procesar y descargar Excel</span>
        </button>
        <progress id="prog" class="bar" value="0" max="100"></progress>
        <div id="status" class="foot"></div>
      </form>
      <div class="foot" style="margin-top:12px;">Tip: se consulta la API de VTEX por EAN; si no hay match, se busca la primera PDP en la página de resultados.</div>
    </div>
  </div>
<script>
const form = document.getElementById('form');
const btn = document.getElementById('btn');
const btnText = document.getElementById('btn-text');
const bar = document.getElementById('prog');
const statusEl = document.getElementById('status');

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!document.getElementById('file').files.length) return;
  toggle(true);
  const fd = new FormData(form);
  try {
    const start = await fetch('/start', { method: 'POST', body: fd });
    if (!start.ok) throw new Error('Error iniciando el procesamiento');
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
    """

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Subí un .xlsx (Excel moderno)")

    # Leer Excel en memoria (forzar str para no perder ceros)
    raw = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(raw), dtype=str)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No pude leer el Excel: {e}")

    if df.empty:
        raise HTTPException(status_code=400, detail="El Excel está vacío")

    # Detectar columna EAN (case-insensitive)
    cols_norm = {c.strip(): c for c in df.columns}
    target_col = None
    for c in cols_norm:
        if c.lower() == "ean":
            target_col = cols_norm[c]
            break
    if target_col is None:
        raise HTTPException(status_code=400, detail="No encontré una columna llamada 'EAN'")

    eans = [sanitize_ean(v) for v in df[target_col].tolist()]

    # Procesar
    out_df = await process_eans(eans)

    # Armar Excel en memoria
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


# --------------------------- Healthcheck sencillo ---------------------------
@app.get("/health")
async def health():
    return {"ok": True, "stores": list(STORES.keys())}
