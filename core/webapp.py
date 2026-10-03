"""WebApp FastAPI: UI + endpoints para el pipeline.

Migrado desde BaseHTTPRequestHandler (single-threaded) a FastAPI/Uvicorn
(concurrencia real). Las cargas largas no bloquean otras requests.

Auth (4.1): si la env var WEBAPP_AUTH_TOKEN está seteada, todas las rutas
NO-públicas requieren `Authorization: Bearer <token>`. Si no está seteada,
el server arranca sin auth (con un warning bien visible en logs).

HTTPS (4.1): el módulo no termina TLS por sí mismo; uvicorn lo hace si se
pasan `--ssl-keyfile` y `--ssl-certfile`. Ver README/.env.example.

Rutas:
    GET  /             página principal con upload + panel bigdata
    POST /upload       sube archivo, ejecuta pipeline, devuelve outputs
    GET  /download/{f} descarga archivo de OUTPUT_DIR
    POST /bigdata/*    handlers de preparación multi-tabla (delegado a bigdata_routes)
    GET  /healthz      healthcheck público (sin auth)
"""

import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import (Depends, FastAPI, File, Header, HTTPException, Request,
                     UploadFile)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.cors import CORSMiddleware

from config.settings import PipelineConfig
from core import bigdata_routes
from core.logging_engine import setup_logger

logger = setup_logger("webapp")

AUTH_TOKEN = os.environ.get("WEBAPP_AUTH_TOKEN", "").strip()


# ─── Auth dependency ────────────────────────────────────────────────────────
def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
    """Si AUTH_TOKEN está seteado, valida Bearer; si no, pass-through."""
    if not AUTH_TOKEN:
        return  # auth deshabilitada
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    # Comparación constant-time para evitar timing attacks
    if not secrets.compare_digest(token, AUTH_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")


PUBLISH_DIR = os.environ.get(
    "DATACLEAN_PUBLISH_DIR",
    r"C:\Users\marku\Downloads\El cubo\data-clean",
)


def _process_upload(file_path: str) -> dict:
    """Flujo completo de una subida: ETL -> SQL local -> tableros -> portada.

    Antes esto sólo corría el ETL y devolvía una lista de archivos para
    descargar. Ahora la subida deja además el dataset consultable en SQL y su
    tablero publicado, que es el punto de todo el flujo: subir un archivo y
    verlo, sin pasos manuales intermedios.
    """
    import auto_pipeline

    result = auto_pipeline.run_pipeline(file_path, PipelineConfig.OUTPUT_DIR)
    run_dir = result["run_dir"]
    out: dict = {
        "run_id": result["run_id"],
        "run_dir": run_dir,
        "quarantine_failed": result["quarantine_failed"],
        "quarantine_ratio": result["q_ratio"],
        "tables": [],
        "dashboards": [],
    }

    from core.sql_export import export_run
    sql = export_run(run_dir)
    out["tables"] = sql.get("tables", [])
    out["sql"] = {k: os.path.basename(v) for k, v in sql.items()
                  if k in ("duckdb", "sqlite", "query_guide") and v}

    if result["quarantine_failed"]:
        # Datos declarados dudosos por el propio pipeline: no se publican.
        logger.warning("Cuarentena sobre el umbral; no se publican tableros")
        return out

    import json as _json

    import pandas as pd

    from analytics import portal, registry

    quality = {}
    qpath = os.path.join(run_dir, "quality_report.json")
    if os.path.exists(qpath):
        with open(qpath, encoding="utf-8") as f:
            quality = _json.load(f)

    for table in out["tables"]:
        parquet = os.path.join(run_dir, f"{table}_clean.parquet")
        if not os.path.exists(parquet):
            continue
        try:
            df = pd.read_parquet(parquet)
            if df.empty:
                continue
            slug = portal.slugify(table)
            meta = registry.build(df, table, os.path.join(PUBLISH_DIR, slug),
                                  run_dir=run_dir, quality=quality,
                                  origen=os.path.basename(file_path))
            out["dashboards"].append({"tabla": table, "slug": slug,
                                       "tipo": meta["tipo"], "filas": meta["filas"]})
        except Exception as e:
            logger.error(f"Tablero de '{table}' falló: {e}")

    if out["dashboards"]:
        portal.build(PUBLISH_DIR)
        out["portal"] = os.path.join(PUBLISH_DIR, "index.html")
    return out


# ─── App ────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def _lifespan(app: FastAPI):
    if AUTH_TOKEN:
        logger.info("[webapp] Bearer auth HABILITADA")
    else:
        logger.warning(
            "[webapp] AUTH DESHABILITADA — cualquier cliente puede subir "
            "archivos y ejecutar el pipeline. Setea WEBAPP_AUTH_TOKEN en .env "
            "para activar bearer auth."
        )
    yield


app = FastAPI(
    title="Data Clean Pipeline",
    description="ETL para preparar archivos limpios listos para SQL.",
    version="1.0.0",
    lifespan=_lifespan,
)

# CORS suelto solo para localhost en dev; producción debe restringir
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8765"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Endpoints públicos ─────────────────────────────────────────────────────
@app.get("/healthz", response_class=JSONResponse)
async def healthcheck():
    return {"status": "ok", "auth_required": bool(AUTH_TOKEN)}


@app.get("/", response_class=HTMLResponse)
async def index():
    """Sirve el HTML principal con upload + panel bigdata."""
    return HTMLResponse(_INDEX_HTML)


# ─── Endpoints autenticados ─────────────────────────────────────────────────
@app.post("/upload", dependencies=[Depends(require_auth)])
async def upload(file: UploadFile = File(...)):
    """Recibe archivo, ejecuta el flujo completo y devuelve qué se produjo."""
    os.makedirs("uploads", exist_ok=True)
    file_path = os.path.join("uploads", os.path.basename(file.filename))
    with open(file_path, "wb") as f:
        while True:
            chunk = await file.read(8192)
            if not chunk:
                break
            f.write(chunk)

    try:
        result = _process_upload(file_path)
    except Exception as e:
        logger.error(f"Pipeline failed on upload: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    run_dir = result["run_dir"]
    files = []
    if os.path.exists(run_dir):
        for root, _, names in os.walk(run_dir):
            for n in names:
                # Relativo a OUTPUT_DIR (incluye el run_id) para que las rutas
                # sirvan tal cual en /download.
                files.append(os.path.relpath(os.path.join(root, n),
                                             PipelineConfig.OUTPUT_DIR)
                             .replace("\\", "/"))
    return {"success": True, "files": sorted(files), **result}


@app.get("/formats", response_class=JSONResponse)
async def formats():
    """Formatos que el pipeline sabe leer. Alimenta la UI."""
    from core.ingest import describe_support
    return describe_support()


@app.get("/download/{filename:path}", dependencies=[Depends(require_auth)])
async def download(filename: str):
    # El path viene del cliente: hay que confinarlo a OUTPUT_DIR. Sin esta
    # comprobación, `../../.env` serviría cualquier archivo del disco.
    root = os.path.realpath(PipelineConfig.OUTPUT_DIR)
    file_path = os.path.realpath(os.path.join(root, filename))
    if not file_path.startswith(root + os.sep):
        raise HTTPException(status_code=403, detail="Path outside output dir")
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, filename=os.path.basename(filename))


@app.post("/bigdata/{action}", dependencies=[Depends(require_auth)])
async def bigdata_dispatch(action: str, request: Request):
    body = await request.body()
    path = f"/bigdata/{action}"
    result = bigdata_routes.dispatch(path, body)
    if result is None:
        raise HTTPException(status_code=404, detail="Unknown bigdata action")
    status = 200 if result.get("success") else 500
    return JSONResponse(result, status_code=status)


# ─── Server runner ──────────────────────────────────────────────────────────
class WebApp:
    @staticmethod
    def serve(port: int = 8765, host: str = "0.0.0.0",
               ssl_keyfile: Optional[str] = None,
               ssl_certfile: Optional[str] = None) -> None:
        import uvicorn
        config = {
            "host": host, "port": port,
            "log_level": "info",
        }
        if ssl_keyfile and ssl_certfile:
            config["ssl_keyfile"] = ssl_keyfile
            config["ssl_certfile"] = ssl_certfile
            scheme = "https"
        else:
            scheme = "http"
        logger.info(f"UI Server: {scheme}://localhost:{port}")
        uvicorn.run("core.webapp:app", **config)


# ─── HTML estático ──────────────────────────────────────────────────────────
_INDEX_HTML = """<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<title>Data Cleaner V4</title>
<style>
body{font-family:Inter,system-ui,sans-serif;background:#0f172a;color:#f8fafc;display:flex;justify-content:center;padding:40px 0;margin:0;}
.container{background:#1e293b;padding:40px;border-radius:12px;width:100%;max-width:600px;}
h1{color:#38bdf8;margin-bottom:10px;}h2{color:#fbbf24;margin-top:30px;}
p{color:#94a3b8;}
.upload-area{border:2px dashed #475569;border-radius:8px;padding:40px 20px;cursor:pointer;text-align:center;margin-bottom:20px;}
.upload-area:hover{border-color:#38bdf8;background:rgba(56,189,248,0.05);}
input[type="file"]{display:none;}
.btn{background:linear-gradient(135deg,#0284c7,#2563eb);color:white;border:none;padding:12px 24px;border-radius:6px;cursor:pointer;width:100%;font-size:16px;font-weight:600;}
.btn:disabled{background:#475569;cursor:not-allowed;}
input[type="text"],input[type="password"]{width:100%;background:#0f172a;color:#f8fafc;border:1px solid #475569;border-radius:6px;padding:10px;font-family:'Consolas',monospace;font-size:13px;box-sizing:border-box;margin-bottom:10px;}
#status,#bigdataResult{margin-top:20px;font-family:'Consolas',monospace;font-size:12px;color:#e2e8f0;}
hr{border:none;border-top:1px solid #334155;margin:30px 0;}
table{border-collapse:collapse;width:100%;font-size:11px;}
th,td{border:1px solid #334155;padding:4px;text-align:left;}
th{background:#1e293b;color:#fbbf24;}
pre{background:#0f172a;padding:8px;border-radius:4px;white-space:pre-wrap;word-break:break-all;}
</style></head><body>
<div class="container">
  <h1>Data Cleaner</h1>
  <p>ETL chunked semántico con auditoría completa.</p>

  <label class="upload-area" for="fileInput">
    <div id="fileInfo">Click o arrastra un archivo</div>
    <div style="color:#64748b;font-size:11px;margin-top:8px;">
      csv · tsv · txt · xlsx · xls · json · jsonl · parquet · sqlite · db · sql · bak · zip
    </div>
    <input type="file" id="fileInput"
           accept=".csv,.tsv,.txt,.dat,.xlsx,.xls,.xlsm,.json,.jsonl,.ndjson,.parquet,.pq,.db,.sqlite,.sqlite3,.sql,.bak,.zip">
  </label>
  <button class="btn" id="btnUpload" disabled>Procesar Archivo</button>
  <div id="status"></div>

  <hr>
  <h2>Dataset multi-tabla</h2>
  <p>Carpeta con TSV/CSV crudos -> Parquet limpio listo para SQL externo.</p>
  <input type="text" id="ddSource" value="ultimo trabajo/imdb_data" placeholder="Source folder">
  <input type="text" id="ddOutput" value="ultimo trabajo/imdb_parquet" placeholder="Output folder">
  <button class="btn" id="btnPrepare" style="background:#0284c7;">Preparar dataset</button>
  <div id="bigdataResult"></div>

  <hr>
  <details>
    <summary style="cursor:pointer;color:#94a3b8;">Token (si auth está habilitada)</summary>
    <input type="password" id="authToken" placeholder="Bearer token" style="margin-top:10px;">
  </details>
</div>

<script>
function authHeaders(extra) {
  const t = document.getElementById('authToken').value.trim();
  const h = Object.assign({}, extra || {});
  if (t) h['Authorization'] = 'Bearer ' + t;
  return h;
}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

const fileInput = document.getElementById('fileInput');
const btnUpload = document.getElementById('btnUpload');
const fileInfo = document.getElementById('fileInfo');
const status = document.getElementById('status');
fileInput.addEventListener('change', () => {
  if (fileInput.files.length > 0) {
    fileInfo.innerHTML = '<strong>' + esc(fileInput.files[0].name) + '</strong>';
    btnUpload.disabled = false;
  }
});
btnUpload.addEventListener('click', async () => {
  btnUpload.disabled = true;
  status.innerHTML = 'Procesando...';
  const fd = new FormData();
  fd.append('file', fileInput.files[0]);
  const r = await fetch('/upload', {method:'POST', body: fd, headers: authHeaders()});
  const data = await r.json();
  if (data.success) {
    let h = '<div style="color:#10b981;">OK — run ' + esc(data.run_id) + '</div>';

    if (data.quarantine_failed) {
      h += '<div style="color:#f59e0b;margin-top:8px;">Cuarentena ' +
           (data.quarantine_ratio*100).toFixed(2) + '% sobre el umbral: ' +
           'no se publicaron tableros. Revisa la carpeta quarantine del run.</div>';
    }

    if (data.tables && data.tables.length) {
      h += '<h3 style="color:#fbbf24;font-size:13px;">Tablas detectadas</h3>';
      h += '<table><tr><th>Tabla</th><th>Tablero</th><th>Filas</th></tr>';
      const byTable = {};
      (data.dashboards || []).forEach(d => byTable[d.tabla] = d);
      data.tables.forEach(t => {
        const d = byTable[t];
        h += '<tr><td>' + esc(t) + '</td><td>' +
             (d ? esc(d.tipo) : '—') + '</td><td>' +
             (d ? d.filas.toLocaleString('es-CL') : '—') + '</td></tr>';
      });
      h += '</table>';
    }

    if (data.sql) {
      h += '<h3 style="color:#fbbf24;font-size:13px;">Consultar en SQL</h3>';
      h += '<pre>duckdb  ' + esc(data.run_dir) + '\\' + esc(data.sql.duckdb || '') +
           '\nsqlite3 ' + esc(data.run_dir) + '\\' + esc(data.sql.sqlite || '') + '</pre>';
    }

    if (data.portal) {
      h += '<h3 style="color:#fbbf24;font-size:13px;">Tableros publicados</h3>';
      h += '<pre>' + esc(data.portal) + '</pre>';
    }

    h += '<h3 style="color:#fbbf24;font-size:13px;">Descargas</h3>';
    h += data.files.map(f =>
      '<a class="btn" style="margin-top:6px;text-decoration:none;display:block;text-align:center;" href="/download/' +
      f.split('/').map(encodeURIComponent).join('/') + '">' + esc(f) + '</a>'
    ).join('');
    status.innerHTML = h;
    btnUpload.disabled = false;
  } else {
    status.innerHTML = '<div style="color:#ef4444;">' + esc(data.error || 'Error') + '</div>';
    btnUpload.disabled = false;
  }
});

const bdResult = document.getElementById('bigdataResult');
document.getElementById('btnPrepare').addEventListener('click', async () => {
  const source = document.getElementById('ddSource').value.trim();
  const output = document.getElementById('ddOutput').value.trim();
  bdResult.innerHTML = '<div style="color:#fbbf24;">Procesando...</div>';
  const r = await fetch('/bigdata/prepare-dataset', {
    method:'POST',
    headers: authHeaders({'Content-Type':'application/json'}),
    body: JSON.stringify({source, output})
  });
  const data = await r.json();
  if (data.success) {
    let h = '<div style="color:#10b981;">OK - ' + esc(data.output) + '</div>';
    h += '<table><tr><th>Tabla</th><th>Filas</th></tr>';
    for (const [k,v] of Object.entries(data.stats)) {
      h += '<tr><td>' + esc(k) + '</td><td>' + v.toLocaleString('es-CL') + '</td></tr>';
    }
    h += '</table>';
    if (data.how_to_query) {
      h += '<h3 style="color:#fbbf24;font-size:13px;">Cómo consultar</h3>';
      for (const [tool, snip] of Object.entries(data.how_to_query)) {
        h += '<div style="color:#94a3b8;">' + esc(tool) + '</div>';
        h += '<pre>' + esc(snip) + '</pre>';
      }
    }
    bdResult.innerHTML = h;
  } else {
    bdResult.innerHTML = '<div style="color:#ef4444;">' + esc(data.error || 'Error') + '</div>';
  }
});
</script></body></html>"""
