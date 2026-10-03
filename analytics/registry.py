"""Elige el dashboard de cada tabla: especializado si aplica, genérico si no.

Mismo criterio que el registry de cleaners (`core/cleaners/__init__.py`): un
dataset conocido recibe el tablero que cuenta su historia; cualquier otro
recibe el auto-perfilado, que nunca sabe de negocio pero siempre funciona.

Agregar un dashboard especializado nuevo = agregar una entrada a SPECIALIZED
con su `matches(columns)` y su par (insights, render).
"""

import json
import os
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from analytics import dashboard as abarrotes_dashboard
from analytics import generic_dashboard
from core.logging_engine import setup_logger

logger = setup_logger("dashboard_registry")


def _abarrotes(df: pd.DataFrame, table: str, run_dir: str,
               quality: Optional[dict], origen: str) -> Tuple[dict, str]:
    from analytics.abarrotes_bi import build_insights
    insights = build_insights(df, run_dir=run_dir, source_file=origen)
    return insights, abarrotes_dashboard.render(insights)


SPECIALIZED: List[Dict] = [
    {
        "name": "abarrotes_cl",
        "title": "Abarrotes CL — ventas y proyección de demanda",
        "matches": abarrotes_dashboard.matches,
        "build": _abarrotes,
    },
]


def select(columns) -> Optional[dict]:
    """Devuelve la definición del dashboard especializado que aplica, o None."""
    for spec in SPECIALIZED:
        try:
            if spec["matches"](columns):
                return spec
        except Exception as e:      # un matcher roto no puede tumbar el run
            logger.debug(f"matcher de {spec['name']} falló: {e}")
    return None


def build(df: pd.DataFrame, table: str, out_dir: str, run_dir: str = "",
          quality: Optional[dict] = None, origen: str = "") -> dict:
    """Construye el tablero de una tabla y deja `index.html` + `meta.json`.

    `meta.json` es lo que después lee la portada para armar el índice sin
    tener que abrir cada HTML.
    """
    os.makedirs(out_dir, exist_ok=True)
    out_html = os.path.join(out_dir, "index.html")

    spec = select(df.columns)
    if spec:
        logger.info(f"Tabla '{table}': dashboard especializado '{spec['name']}'")
        insights, html = spec["build"](df, table, run_dir, quality, origen)
        with open(out_html, "w", encoding="utf-8") as f:
            f.write(html)
        titulo = spec["title"]
        kind = spec["name"]
        resumen = _abarrotes_summary(insights)
    else:
        logger.info(f"Tabla '{table}': dashboard genérico auto-perfilado")
        insights = generic_dashboard.build(
            df, table, out_html, run_dir=run_dir, quality=quality, origen=origen)
        titulo = table
        kind = "generico"
        resumen = _generic_summary(insights)

    meta = {
        "tabla": table, "titulo": titulo, "tipo": kind,
        "run_id": os.path.basename(run_dir.rstrip("/\\")) if run_dir else "",
        "origen": origen,
        "filas": int(len(df)), "columnas": int(len(df.columns)),
        "generado": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "resumen": resumen,
    }
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False, default=str)

    # El insights del tablero queda junto al run para poder auditarlo.
    if run_dir and os.path.isdir(run_dir):
        with open(os.path.join(run_dir, f"insights_{table}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(insights, f, indent=2, ensure_ascii=False, default=str)

    return meta


def _abarrotes_summary(insights: dict) -> List[dict]:
    k, p = insights["kpis"], insights["proyeccion"]
    return [
        {"label": "Venta 2019-2022", "value": f"MM$ {k['venta_total']/1e6:,.1f}"
            .replace(",", "@").replace(".", ",").replace("@", ".")},
        {"label": "Margen", "value": f"{k['margen_pct']:.2f}%".replace(".", ",")},
        {"label": f"Proyección {p['anio_proyectado']}",
         "value": f"{p['unidades_total']:,} u.".replace(",", ".")},
    ]


def _generic_summary(insights: dict) -> List[dict]:
    meta, medidas = insights["meta"], insights.get("medidas", [])
    out = [{"label": "Filas", "value": f"{meta['filas']:,}".replace(",", ".")},
           {"label": "Columnas", "value": str(meta["columnas"])}]
    if medidas:
        m = medidas[0]
        total = m.get("sum", 0)
        out.append({"label": f"Total {m['columna']}", "value": _compact(total)})
    serie = insights.get("serie")
    if serie:
        out.append({"label": "Período", "value": f"{serie['desde']} → {serie['hasta']}"})
    return out


def _compact(v: float) -> str:
    for limit, suffix in ((1e9, " B"), (1e6, " M"), (1e4, " K")):
        if abs(v) >= limit:
            return f"{v/limit:,.1f}".replace(",", "@").replace(".", ",").replace("@", ".") + suffix
    return f"{v:,.0f}".replace(",", ".")
