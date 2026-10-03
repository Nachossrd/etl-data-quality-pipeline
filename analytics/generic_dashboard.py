"""Dashboard auto-perfilado: sirve para un dataset que nunca vimos.

Toma la salida limpia de cualquier tabla del pipeline, la perfila con
`analytics/profiler.py` y decide **solo** qué mostrar:

    - hay una fecha  -> serie de tiempo de las medidas principales
    - hay medidas    -> totales, distribución y relación entre las dos mayores
    - hay dimensiones-> rankings top-N por la medida principal (o por conteo)
    - siempre        -> perfil de columnas y hallazgos de calidad

Cuando el dataset no tiene alguna de esas piezas, la sección correspondiente
simplemente no se dibuja: un catálogo sin fechas ni montos igual produce un
tablero útil (conteos y cardinalidades) en vez de un gráfico vacío.

Nada de esto sabe de negocio. Para un caso con narrativa propia existe el
dashboard especializado (ver `analytics/registry.py`).
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from analytics.profiler import (_DATE_TOKENS, DatasetProfile, _name_has,
                                profile_dataset)
from analytics.viz_kit import render_page
from core.logging_engine import setup_logger

logger = setup_logger("generic_dashboard")

TOP_N = 12
MAX_DIMENSION_CHARTS = 6
HISTOGRAM_BINS = 24


# ─────────────────────────── agregaciones ──────────────────────────────────
def _grain_for(span_days: float) -> str:
    """Granularidad temporal legible según el rango cubierto."""
    if span_days <= 3:
        return "hour"
    if span_days <= 120:
        return "day"
    if span_days <= 365 * 3:
        return "month"
    if span_days <= 365 * 25:
        return "month"
    return "year"


_FREQ = {"hour": "h", "day": "D", "month": "MS", "year": "YS"}
_FMT = {"hour": "%Y-%m-%d %H:00", "day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}


def _time_series(df: pd.DataFrame, date_col: str, measures: List[str]) -> Optional[dict]:
    dates = pd.to_datetime(df[date_col], errors="coerce")
    valid = dates.notna()
    if valid.sum() < 2:
        return None

    span = (dates.max() - dates.min()).days or 1
    grain = _grain_for(span)
    keys = dates[valid].dt.to_period(
        {"hour": "h", "day": "D", "month": "M", "year": "Y"}[grain]
    ).dt.to_timestamp()

    frame = df.loc[valid].copy()
    frame["_k"] = keys.values
    agg = {m: (m, "sum") for m in measures}
    agg["_filas"] = (date_col, "size")
    grouped = frame.groupby("_k", observed=True).agg(**agg).sort_index()

    return {
        "columna": date_col,
        "granularidad": grain,
        "desde": dates.min().strftime("%Y-%m-%d"),
        "hasta": dates.max().strftime("%Y-%m-%d"),
        "medidas": measures,
        "puntos": [
            {"x": ts.strftime(_FMT[grain]),
             "filas": int(row["_filas"]),
             **{m: (None if pd.isna(row[m]) else round(float(row[m]), 4)) for m in measures}}
            for ts, row in grouped.iterrows()
        ],
    }


def _dimension_breakdown(df: pd.DataFrame, dim: str,
                         measure: Optional[str]) -> dict:
    if measure:
        grouped = df.groupby(dim, observed=True, dropna=False).agg(
            valor=(measure, "sum"), filas=(dim, "size"))
        metric_label, metric = measure, "suma"
    else:
        grouped = df.groupby(dim, observed=True, dropna=False).agg(
            valor=(dim, "size"), filas=(dim, "size"))
        metric_label, metric = "filas", "conteo"

    total = float(grouped["valor"].sum()) or 1.0
    grouped = grouped.sort_values("valor", ascending=False)
    top = grouped.head(TOP_N)

    items = [
        {"valor_dim": "(sin dato)" if pd.isna(idx) else str(idx),
         "valor": round(float(r["valor"]), 4),
         "filas": int(r["filas"]),
         "pct": round(float(r["valor"]) / total * 100, 2)}
        for idx, r in top.iterrows()
    ]
    resto = len(grouped) - len(top)
    return {
        "columna": dim, "metrica": metric, "medida": metric_label,
        "uniques": int(len(grouped)), "otros": int(resto),
        "otros_valor": round(float(grouped["valor"].iloc[TOP_N:].sum()), 4) if resto > 0 else 0.0,
        "total": round(total, 4),
        "items": items,
    }


def _histogram(series: pd.Series, bins: int = HISTOGRAM_BINS) -> Optional[dict]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if len(values) < 10 or values.nunique() < 3:
        return None
    # Recorte al 1-99% para que un outlier no aplaste toda la distribución.
    lo, hi = values.quantile(0.01), values.quantile(0.99)
    if lo == hi:
        lo, hi = values.min(), values.max()
    if lo == hi:
        return None
    clipped = values.clip(lo, hi)
    counts, edges = np.histogram(clipped, bins=bins)
    return {
        "columna": str(series.name),
        "recortado": bool(lo > values.min() or hi < values.max()),
        "bins": [
            {"desde": round(float(edges[i]), 4), "hasta": round(float(edges[i + 1]), 4),
             "conteo": int(counts[i])}
            for i in range(len(counts))
        ],
    }


def _dimension_score(uniques: int) -> float:
    """Qué tan buena es una columna para graficar, según su cardinalidad.

    Ni 2 valores (un gráfico de dos barras dice poco) ni 5.000 (ilegible). La
    banda útil está alrededor de una decena de categorías, así que se puntúa
    por distancia logarítmica a ese ideal. Ordenar por cardinalidad ascendente
    —lo obvio— deja arriba justamente las columnas menos informativas.
    """
    if uniques < 2:
        return -99.0
    return -abs(np.log(uniques) - np.log(10))


def _drop_redundant(df: pd.DataFrame, dims: List[str]) -> List[str]:
    """Descarta dimensiones que son la misma información con otro nombre.

    `formato` y `formato_gramos`, o `mes` y `mes_nombre`, producen gráficos
    idénticos con etiquetas distintas. Si cada valor de A determina uno solo
    de B y viceversa, es la misma columna: se conserva la primera.
    """
    kept: List[str] = []
    for dim in dims:
        redundant = False
        for prev in kept:
            try:
                pairs = df[[prev, dim]].dropna().drop_duplicates()
                if (len(pairs) == df[prev].nunique(dropna=True)
                        and len(pairs) == df[dim].nunique(dropna=True)):
                    redundant = True
                    break
            except (TypeError, KeyError):
                continue
        if not redundant:
            kept.append(dim)
    return kept


def _scatter(df: pd.DataFrame, dim: str, x: str, y: str) -> Optional[dict]:
    grouped = df.groupby(dim, observed=True).agg(
        x=(x, "sum"), y=(y, "sum"), filas=(dim, "size")
    ).reset_index()
    grouped = grouped[(grouped["x"] > 0) & (grouped["y"] > 0)]
    if len(grouped) < 4:
        return None
    corr = float(grouped["x"].corr(grouped["y"]))
    grouped = grouped.nlargest(120, "y")
    return {
        "dimension": dim, "x": x, "y": y,
        "correlacion": None if pd.isna(corr) else round(corr, 4),
        "puntos": [
            {"label": str(r[dim]), "x": round(float(r["x"]), 4),
             "y": round(float(r["y"]), 4), "filas": int(r["filas"])}
            for _, r in grouped.iterrows()
        ],
    }


# ─────────────────────────────── insights ──────────────────────────────────
def build_insights(df: pd.DataFrame, table: str, run_dir: str = "",
                   quality: Optional[dict] = None,
                   origen: str = "") -> dict:
    profile = profile_dataset(df, table)
    measures = profile.measures
    primary_measure = measures[0] if measures else None

    uniques_of = {c.name: c.uniques for c in profile.columns}
    # Entre dos columnas equivalentes (`region` vs `region_codigo`) gana la
    # legible: en el eje de un gráfico "Valparaíso" dice más que "V".
    def _dim_key(d: str):
        penalty = 0.15 if any(t in d.lower() for t in ("codigo", "code", "_id", "cod_")) else 0
        return _dimension_score(uniques_of.get(d, 0)) - penalty

    dimensions = _drop_redundant(df, sorted(profile.dimensions, key=_dim_key, reverse=True))

    quality = quality or {}
    table_quality = (quality.get("tables", {}) or {}).get(table, {})
    totals = table_quality.get("row_counts", {}) or quality.get("totals", {}) or {}

    # ── Serie de tiempo (hasta 3 medidas, cada una en su propio gráfico:
    #    dos escalas en un mismo eje mienten sobre la relación entre ellas) ──
    serie = None
    if profile.primary_date:
        serie = _time_series(df, profile.primary_date, measures[:3])

    # ── Rankings por dimensión ────────────────────────────────────────────
    breakdowns = [
        _dimension_breakdown(df, dim, primary_measure)
        for dim in dimensions[:MAX_DIMENSION_CHARTS]
    ]

    # ── Distribución y relación ───────────────────────────────────────────
    distribucion = _histogram(df[primary_measure]) if primary_measure else None
    dispersion = None
    if len(measures) >= 2 and dimensions:
        # Al revés que en los rankings: aquí conviene la dimensión con MÁS
        # valores dentro de lo graficable — una nube de 12 puntos no muestra
        # dispersión, sólo una recta. El techo mantiene cada punto identificable.
        # Se excluyen las dimensiones temporales: el tiempo ya tiene su propia
        # sección, y una nube de meses no dice nada que la serie no diga mejor.
        candidatos = [d for d in dimensions
                      if 6 <= uniques_of.get(d, 0) <= 120
                      and not _name_has(d, _DATE_TOKENS)]
        if candidatos:
            dim = max(candidatos, key=lambda d: uniques_of.get(d, 0))
            dispersion = _scatter(df, dim, measures[1], measures[0])

    # Ordenadas por importancia (magnitud total), no por posición en el archivo:
    # el titular del tablero debe ser la medida que mueve la aguja.
    by_name = {c.name: c for c in profile.columns}
    resumen_medidas = [
        {"columna": name,
         **{k: round(v, 4) if isinstance(v, float) else v
            for k, v in by_name[name].stats.items()},
         "nulos": by_name[name].nulls,
         "null_pct": round(by_name[name].null_ratio * 100, 2)}
        for name in measures
    ]

    return {
        "meta": {
            "tabla": table,
            "generado": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "run_id": os.path.basename(run_dir.rstrip("/\\")) if run_dir else "",
            "origen": origen,
            "filas": int(len(df)),
            "columnas": int(len(df.columns)),
            "filas_extraidas": int(totals.get("extracted", len(df))),
            "filas_limpias": int(totals.get("cleaned", len(df))),
            "filas_cuarentena": int(totals.get("quarantined", 0)),
            "ratio_cuarentena": float(totals.get("quarantine_ratio", 0.0)),
        },
        "perfil": profile.to_dict(),
        "serie": serie,
        "dimensiones": breakdowns,
        "medidas": resumen_medidas,
        "distribucion": distribucion,
        "dispersion": dispersion,
        "calidad": {
            "flags": table_quality.get("flag_breakdown", {}),
            "violaciones": table_quality.get("rule_violations", {}),
            "score": table_quality.get("quality_score_distribution", {}),
        },
    }


# ──────────────────────────────── render ───────────────────────────────────
HEADER = """  <header class="top">
    <div class="eyebrow">Data Clean · tablero automático</div>
    <h1 id="titulo"></h1>
    <p class="sub">Este tablero se construyó solo: el pipeline perfiló cada columna, dedujo su
    rol —fecha, medida, categoría o llave— y eligió qué mostrar. Ninguna cifra está escrita a
    mano; todas salen del dataset limpio de este run.</p>
    <div class="meta-row" id="meta"></div>
  </header>"""

FOOTER = """<div>Tablero generado automáticamente por Data Clean · tabla <code id="tabla"></code>
    · run <code id="runid"></code> · <span id="gen"></span></div>
    <div>Los datos limpios de este run están en CSV, Parquet, DuckDB y SQLite junto a este archivo.</div>"""

BUILD_JS = r"""
const M = DATA.meta, PR = DATA.perfil;
const ROLES = {date:'fecha', measure:'medida', dimension:'categoría',
               identifier:'llave', flag:'indicador', text:'texto libre', constant:'constante'};

document.title = 'Data Clean — ' + M.tabla;
document.getElementById('titulo').textContent = M.tabla;
document.getElementById('tabla').textContent = M.tabla;
document.getElementById('runid').textContent = M.run_id || '—';
document.getElementById('gen').textContent = M.generado;

const okPct = 100 - (M.ratio_cuarentena || 0) * 100;
document.getElementById('meta').innerHTML = [
  `<span class="pill">${num(M.filas)} filas × ${M.columnas} columnas</span>`,
  DATA.serie ? `<span class="pill">${DATA.serie.desde} → ${DATA.serie.hasta}</span>` : '',
  `<span class="pill"><span class="dot" style="background:var(--${okPct >= 99 ? 'good' : 'warning'})"></span>${pct(okPct, 2)} de filas utilizables</span>`,
  M.origen ? `<span class="pill">${esc(M.origen)}</span>` : '',
].join('');

/* ── 1 · Titulares ───────────────────────────────────────────────────── */
const medidas = PR.measures, dims = PR.dimensions, ids = PR.identifiers;
app.appendChild(h(`<h2>1 · Qué contiene este dataset</h2>
  <p class="lede">${num(M.filas)} filas y ${M.columnas} columnas.
  El perfilador encontró ${medidas.length} medida(s), ${dims.length} categoría(s)
  ${PR.primary_date ? 'y un eje temporal (<code>' + esc(PR.primary_date) + '</code>)' : 'y ningún eje temporal'}.</p>`));

const principal = DATA.medidas[0];
if (principal) {
  app.appendChild(h(`<div class="card hero">
    <div>
      <div class="hero-fig">${compact(principal.sum)}</div>
      <div class="hero-label">Total de <strong>${esc(principal.columna)}</strong> · ${num(M.filas)} filas</div>
    </div>
    <div style="flex:1 1 220px">
      <div class="tile-label">Promedio por fila</div>
      <div class="tile-value">${compact(principal.mean)}</div>
      <div class="tile-note">mediana ${compact(principal.p50)} · máximo ${compact(principal.max)}</div>
    </div>
  </div>`));
}

const tiles = [
  ['Filas procesadas', num(M.filas_extraidas), 'del archivo original'],
  ['Filas utilizables', num(M.filas_limpias), pct(okPct, 2) + ' del total'],
  ['En cuarentena', num(M.filas_cuarentena), M.filas_cuarentena ? 'revisar antes de confiar' : 'ninguna fila dudosa'],
  ['Columnas', String(M.columnas), `${medidas.length} medidas · ${dims.length} categorías · ${ids.length} llaves`],
];
DATA.medidas.slice(0, 4).forEach(m =>
  tiles.push([`Total ${m.columna}`, compact(m.sum), `promedio ${compact(m.mean)}`]));
app.appendChild(h(`<div class="grid g4" style="margin-top:16px">${tiles.map(([l,v,n]) =>
  `<div class="card"><div class="tile-label">${esc(l)}</div>
   <div class="tile-value">${v}</div><div class="tile-note">${esc(n)}</div></div>`).join('')}</div>`));

/* ── 2 · Evolución temporal ──────────────────────────────────────────── */
if (DATA.serie && DATA.serie.puntos.length > 1) {
  const S = DATA.serie;
  const grano = {hour:'por hora', day:'por día', month:'por mes', year:'por año'}[S.granularidad];
  app.appendChild(h(`<h2>2 · Evolución en el tiempo</h2>
    <p class="lede">Serie ${grano} según <code>${esc(S.columna)}</code>, de ${S.desde} a ${S.hasta}.
    Cada medida va en su propio gráfico: superponer dos escalas distintas en un mismo eje
    haría parecer relacionadas cosas que no lo están.</p>`));

  const seriesToPlot = S.medidas.length ? S.medidas : ['filas'];
  const grid = h('<div class="grid ' + (seriesToPlot.length > 1 ? 'g2' : 'g2') + '"></div>');
  seriesToPlot.forEach(mname => {
    const pts = S.puntos.map(p => ({x: p.x, y: p[mname] ?? 0}));
    const c = card(esc(mname) + ' ' + grano, 'Suma de <code>' + esc(mname) + '</code> por período',
      {table: table(['Período', mname, 'Filas'],
        S.puntos.map(p => [p.x, compact(p[mname] ?? 0), num(p.filas)]))});
    grid.appendChild(c.node);
    draw(c.chart, host => lineChart(host, {
      series: [{name: mname, color: '--series-1', points: pts}],
      fmtY: compact, fmtTip: v => compact(v) ,
      labelEvery: Math.max(1, Math.ceil(S.puntos.length / 8)),
      labelFn: x => x,
    }));
  });
  const cf = card('Filas ' + grano, 'Cuántos registros trae cada período — detecta huecos de carga',
    {table: table(['Período','Filas'], S.puntos.map(p => [p.x, num(p.filas)]))});
  grid.appendChild(cf.node);
  draw(cf.chart, host => columnChart(host, {
    data: S.puntos.map(p => ({label: p.x, value: p.filas})),
    fmtY: compact, fmtTip: v => num(v) + ' filas',
  }));
  app.appendChild(grid);
}

/* ── 3 · Rankings por categoría ──────────────────────────────────────── */
if (DATA.dimensiones.length) {
  const usa = DATA.dimensiones[0].metrica === 'suma'
    ? 'Ordenado por la suma de <code>' + esc(DATA.dimensiones[0].medida) + '</code>.'
    : 'Ordenado por cantidad de filas (el dataset no tiene medidas numéricas que sumar).';
  const topN = DATA.dimensiones[0].items.length;
  app.appendChild(h(`<h2>3 · Desglose por categoría</h2>
    <p class="lede">${usa} Se muestran las ${topN} categorías más grandes de cada columna;
    el resto queda agrupado en la tabla.</p>`));

  const grid = h('<div class="grid g2"></div>');
  DATA.dimensiones.forEach(d => {
    const sub = `${num(d.uniques)} valores distintos` +
                (d.otros > 0 ? ` · se muestran los ${d.items.length} mayores` : '');
    const c = card(esc(d.columna), sub, {
      table: table([d.columna, d.metrica === 'suma' ? d.medida : 'filas', '%', 'Filas'],
        d.items.map(r => [esc(r.valor_dim), compact(r.valor), pct(r.pct), num(r.filas)])
          .concat(d.otros > 0 ? [[`<em>otros ${num(d.otros)} valores</em>`,
                                  compact(d.otros_valor),
                                  pct(d.otros_valor / d.total * 100), '—']] : []))});
    grid.appendChild(c.node);
    draw(c.chart, host => barChart(host, {
      data: d.items.map(r => ({label: r.valor_dim, value: r.valor, p: r.pct, filas: r.filas})),
      fmtVal: v => compact(v),
      fmtTip: (v, r) => `${compact(v)}<div>${pct(r.p)} del total · ${num(r.filas)} filas</div>`,
    }));
  });
  app.appendChild(grid);
}

/* ── 4 · Distribución y relación ─────────────────────────────────────── */
if (DATA.distribucion || DATA.dispersion) {
  app.appendChild(h(`<h2>4 · Cómo se reparten los valores</h2>
    <p class="lede">La distribución muestra dónde se concentran los datos; la dispersión,
    si dos medidas se mueven juntas.</p>`));
  const grid = h('<div class="grid g2"></div>');

  if (DATA.distribucion) {
    const D = DATA.distribucion;
    const c = card('Distribución de ' + esc(D.columna),
      'Cuántas filas caen en cada rango' + (D.recortado ? ' · recortado al 1-99% para que los extremos no aplasten la escala' : ''),
      {table: table(['Desde','Hasta','Filas'],
        D.bins.map(b => [compact(b.desde), compact(b.hasta), num(b.conteo)]))});
    grid.appendChild(c.node);
    draw(c.chart, host => columnChart(host, {
      data: D.bins.map(b => ({label: compact(b.desde), value: b.conteo,
                              full: compact(b.desde) + ' a ' + compact(b.hasta)})),
      fmtY: compact, fmtTip: v => num(v) + ' filas',
    }));
  }

  if (DATA.dispersion) {
    const S = DATA.dispersion;
    const rel = S.correlacion === null ? '' :
      ` Correlación de ${nf2.format(S.correlacion)}: ` +
      (Math.abs(S.correlacion) > 0.8 ? 'se mueven casi en bloque; lo informativo son los puntos que se salen de la nube.'
       : Math.abs(S.correlacion) > 0.4 ? 'hay relación, pero con dispersión real.'
       : 'no se mueven juntas.');
    const c = card(`${esc(S.y)} vs ${esc(S.x)}`,
      `Cada punto es un valor de <code>${esc(S.dimension)}</code>.` + rel,
      {table: table([S.dimension, S.x, S.y, 'Filas'],
        S.puntos.slice(0, 40).map(p => [esc(p.label), compact(p.x), compact(p.y), num(p.filas)]))});
    grid.appendChild(c.node);
    draw(c.chart, host => scatterChart(host, {
      points: S.puntos, xLabel: S.x, yLabel: S.y,
      fmtX: compact, fmtY: compact,
      fmtTip: p => `${esc(S.x)}: ${compact(p.x)}<div>${esc(S.y)}: ${compact(p.y)}</div>
                    <div>${num(p.filas)} filas</div>`,
    }));
  }
  app.appendChild(grid);
}

/* ── 5 · Perfil de columnas y calidad ────────────────────────────────── */
app.appendChild(h(`<h2>5 · Perfil de columnas</h2>
  <p class="lede">Qué entendió el pipeline de cada columna. Este es el criterio con el que
  eligió los gráficos de arriba: si algo está mal clasificado, se corrige aquí primero.</p>`));

const filasPerfil = PR.columns.map(c => [
  `<code>${esc(c.name)}</code>` + (c.internal ? ' <span class="role">interna</span>' : ''),
  `<span class="role">${ROLES[c.role] || c.role}</span>`,
  esc(c.dtype),
  c.uniques < 0 ? '—' : num(c.uniques),
  pct(c.null_ratio * 100, 1),
  c.role === 'measure'
    ? `${compact(c.stats.min)} … ${compact(c.stats.max)}`
    : (c.top_values || []).slice(0, 3).map(v => esc(trunc(v.value, 18))).join(' · ') || '—',
]);
app.appendChild(h(`<div class="card"><div class="scroll">${
  table(['Columna','Rol','Tipo','Valores distintos','Nulos','Rango / valores frecuentes'], filasPerfil)
}</div></div>`));

const flags = Object.entries(DATA.calidad.flags || {});
const viol = Object.entries(DATA.calidad.violaciones || {});
if (flags.length || viol.length || M.filas_cuarentena) {
  app.appendChild(h(`<div class="card" style="margin-top:16px">
    <p class="chart-title">Hallazgos de calidad</p>
    <p class="chart-sub">Marcas puestas por el motor de limpieza y por las reglas declarativas</p>
    <div class="scroll">${table(['Hallazgo','Filas','Tipo'],
      flags.map(([k,v]) => [esc(k), num(v), 'marca de limpieza'])
      .concat(viol.map(([k,v]) => [esc(k), num(v), 'regla declarativa']))
      .concat(M.filas_cuarentena ? [['filas en cuarentena', num(M.filas_cuarentena), 'aisladas del dataset']] : [])
    )}</div>
  </div>`));
} else {
  app.appendChild(h(`<div class="callout" style="margin-top:16px">
    El motor de limpieza no marcó ninguna fila: no encontró fechas ambiguas, montos
    ilegibles ni violaciones de reglas en esta tabla.</div>`));
}
"""


def render(insights: dict, standalone: bool = True) -> str:
    return render_page(
        title=f"Data Clean — {insights['meta']['tabla']}",
        header_html=HEADER, build_js=BUILD_JS, data=insights,
        footer_html=FOOTER, standalone=standalone,
    )


def build(df: pd.DataFrame, table: str, out_path: str, run_dir: str = "",
          quality: Optional[dict] = None, origen: str = "") -> dict:
    """Perfila, arma insights y escribe el HTML. Devuelve los insights."""
    insights = build_insights(df, table, run_dir=run_dir, quality=quality, origen=origen)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(render(insights))
    logger.info(f"Dashboard genérico de '{table}' -> {out_path}")
    return insights
