"""Portada de El cubo: el índice de todo lo que Data Clean ha procesado.

Escanea la carpeta de publicación, lee el `meta.json` que dejó cada tablero y
arma un `index.html` con una tarjeta por dataset. Se regenera completa en cada
run, así que agregar un dataset nuevo no requiere tocar nada: aparece solo.

Convención de carpetas:

    <publicacion>/
        index.html          <- esta portada
        <tabla>/index.html  <- el tablero de esa tabla
        <tabla>/meta.json   <- lo que esta portada lee
"""

import json
import os
import re
import unicodedata
from typing import Dict, List

from analytics.viz_kit import render_page
from core.logging_engine import setup_logger

logger = setup_logger("portal")


def slugify(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    text = re.sub(r"[^0-9A-Za-z]+", "-", text).strip("-").lower()
    return text or "dataset"


def collect(publish_dir: str) -> List[Dict]:
    """Lee el meta.json de cada subcarpeta. El más reciente primero."""
    datasets = []
    if not os.path.isdir(publish_dir):
        return datasets
    for name in sorted(os.listdir(publish_dir)):
        meta_path = os.path.join(publish_dir, name, "meta.json")
        if not os.path.exists(meta_path):
            continue
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"meta.json ilegible en '{name}': {e}")
            continue
        meta["carpeta"] = name
        datasets.append(meta)
    datasets.sort(key=lambda m: m.get("generado", ""), reverse=True)
    return datasets


HEADER = """  <header class="top">
    <div class="eyebrow">Data Clean · El cubo</div>
    <h1>Datasets procesados</h1>
    <p class="sub">Cada tarjeta es un archivo que pasó por el pipeline: limpiado, auditado,
    exportado a SQL y publicado con su propio tablero. Se actualiza sola en cada ejecución —
    subir un archivo nuevo lo hace aparecer acá.</p>
    <div class="meta-row" id="meta"></div>
  </header>"""

FOOTER = """<div>Generado por Data Clean · <span id="gen"></span></div>
    <div>Cada run deja además el dataset en CSV, Parquet, DuckDB y SQLite para consultarlo
    desde Power BI, DBeaver o la herramienta que prefieras.</div>"""

BUILD_JS = r"""
const DS = DATA.datasets;
document.getElementById('gen').textContent = DATA.generado;
document.getElementById('meta').innerHTML = [
  `<span class="pill">${DS.length} dataset(s)</span>`,
  `<span class="pill">${num(DATA.total_filas)} filas procesadas en total</span>`,
].join('');

if (!DS.length) {
  app.appendChild(h(`<div class="callout warn">Todavía no hay datasets publicados.
    Ejecuta <code>python run_data_clean.py --input &lt;archivo&gt;</code> y esta portada
    se llenará sola.</div>`));
} else {
  const grid = h('<div class="grid g2"></div>');
  DS.forEach(d => {
    const chips = (d.resumen || []).map(r =>
      `<div style="margin-top:8px">
         <div class="tile-label">${esc(r.label)}</div>
         <div class="tile-value" style="font-size:19px">${esc(r.value)}</div>
       </div>`).join('');
    grid.appendChild(h(`<a class="cardlink" href="${encodeURIComponent(d.carpeta)}/index.html">
      <div class="card">
        <p class="chart-title">${esc(d.titulo || d.tabla)}</p>
        <p class="chart-sub">
          ${esc(d.origen || d.tabla)} · ${num(d.filas)} filas × ${d.columnas} columnas
          ${d.tipo !== 'generico' ? ' · <span class="role">tablero especializado</span>' : ''}
        </p>
        <div style="display:flex;flex-wrap:wrap;gap:22px">${chips}</div>
        <p class="chart-sub" style="margin:14px 0 0">
          run <code>${esc(d.run_id || '—')}</code> · ${esc(d.generado)}</p>
      </div></a>`));
  });
  app.appendChild(grid);

  app.appendChild(h(`<h2>Todos los datasets</h2>
    <p class="lede">La misma información en tabla, para buscar rápido.</p>`));
  app.appendChild(h(`<div class="card"><div class="scroll">${table(
    ['Dataset','Origen','Filas','Columnas','Tablero','Run','Generado'],
    DS.map(d => [
      `<a href="${encodeURIComponent(d.carpeta)}/index.html">${esc(d.titulo || d.tabla)}</a>`,
      esc(d.origen || '—'), num(d.filas), String(d.columnas),
      d.tipo === 'generico' ? 'automático' : esc(d.tipo),
      `<code>${esc(d.run_id || '—')}</code>`, esc(d.generado),
    ]))}</div></div>`));
}
"""


def build(publish_dir: str) -> str:
    datasets = collect(publish_dir)
    data = {
        "datasets": datasets,
        "total_filas": sum(int(d.get("filas", 0)) for d in datasets),
        "generado": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    html = render_page(
        title="Data Clean — Datasets procesados",
        header_html=HEADER, build_js=BUILD_JS, data=data, footer_html=FOOTER,
    )
    os.makedirs(publish_dir, exist_ok=True)
    path = os.path.join(publish_dir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"Portada actualizada: {path} ({len(datasets)} dataset(s))")
    return path
