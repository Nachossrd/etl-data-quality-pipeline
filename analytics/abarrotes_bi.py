"""Insights de negocio para el caso Abarrotes CL.

Lee la salida limpia del pipeline (Parquet del run vigente) y produce un único
diccionario `insights` — la fuente de verdad tanto del documento de respuestas
como del dashboard. Nada se calcula dos veces en dos lugares distintos: si un
número aparece en el dashboard y en el informe, sale de la misma línea de código.

Uso:
    python -m analytics.abarrotes_bi                 # usa el último run
    python -m analytics.abarrotes_bi --run-dir ...   # un run específico
"""

import argparse
import json
import os
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from analytics.forecasting import MODEL_LABELS, safety_stock, select_and_forecast
from core.logging_engine import setup_logger

logger = setup_logger("abarrotes_bi")

DEFAULT_OUTPUT_ROOT = "./output"
SERVICE_LEVEL = 0.95
TOP_N = 15


# ─────────────────────────── carga ────────────────────────────────────────
def resolve_run_dir(output_root: str = DEFAULT_OUTPUT_ROOT,
                    run_dir: Optional[str] = None) -> str:
    if run_dir:
        return run_dir
    marker = os.path.join(output_root, "latest.txt")
    if os.path.exists(marker):
        with open(marker, encoding="utf-8") as f:
            return os.path.join(output_root, f.read().strip())
    runs = sorted(
        d for d in os.listdir(output_root)
        if os.path.isdir(os.path.join(output_root, d))
    )
    if not runs:
        raise FileNotFoundError(f"No hay runs del pipeline en {output_root}")
    return os.path.join(output_root, runs[-1])


def load_clean_dataset(run_dir: str) -> pd.DataFrame:
    """Carga el dataset limpio del run. Prefiere Parquet (tipos preservados)."""
    parquets = [f for f in os.listdir(run_dir) if f.endswith("_clean.parquet")]
    if parquets:
        path = os.path.join(run_dir, parquets[0])
        logger.info(f"Cargando {path}")
        return pd.read_parquet(path)

    csvs = [f for f in os.listdir(run_dir) if f.endswith("_clean.csv")]
    if not csvs:
        raise FileNotFoundError(f"El run {run_dir} no tiene dataset limpio")
    path = os.path.join(run_dir, csvs[0])
    logger.info(f"Cargando {path} (fallback CSV)")
    return pd.read_csv(path, low_memory=False)


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────── helpers de agregación ────────────────────────────
def _agg(df: pd.DataFrame, by) -> pd.DataFrame:
    grouped = df.groupby(by, dropna=False, observed=True).agg(
        venta=("venta_clp", "sum"),
        unidades=("unidades", "sum"),
        costo=("costo_clp", "sum"),
        lineas=("venta_clp", "size"),
    )
    grouped["margen"] = grouped["venta"] - grouped["costo"]
    grouped["margen_pct"] = np.where(
        grouped["venta"] != 0, grouped["margen"] / grouped["venta"] * 100, np.nan
    )
    return grouped.reset_index()


def _coerce(value):
    """A JSON: números como números, fechas ISO, nulos como None.

    Los dtypes nullable de pandas (Int64/string) devuelven `int` y `pd.NA` de
    Python, no tipos numpy — sin esta rama los enteros terminaban serializados
    como texto y los gráficos del dashboard recibían strings.
    """
    if value is None or value is pd.NA:
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, bool):
        return value
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if pd.isna(value) else round(float(value), 2)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _records(frame: pd.DataFrame, sort_by: str = "venta",
             top: Optional[int] = None) -> List[dict]:
    frame = frame.sort_values(sort_by, ascending=False)
    if top:
        frame = frame.head(top)
    return [{k: _coerce(v) for k, v in row.items()}
            for row in frame.to_dict(orient="records")]


def _canonical_client_names(df: pd.DataFrame) -> pd.Series:
    """Un nombre por código de cliente: el más frecuente.

    El mismo local aparece escrito de varias formas a lo largo de los meses
    (`CORONEL MANUEL MONTT`, `SISA SANTIAGO PORTUGAL`, con y sin prefijo de
    código). La llave de negocio es el código; el nombre es sólo la etiqueta,
    y se elige la variante más usada para que el ranking no parta al cliente.
    """
    modes = (
        df.dropna(subset=["cliente_codigo"])
        .groupby("cliente_codigo", observed=True)["cliente_nombre"]
        .agg(lambda s: s.dropna().mode().iloc[0] if not s.dropna().empty else pd.NA)
    )
    return modes


# ───────────────────────────── insights ───────────────────────────────────
def build_insights(df: pd.DataFrame, run_dir: str = "",
                   source_file: str = "") -> dict:
    df = df.copy()
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    for col in ("venta_clp", "costo_clp", "unidades", "margen_clp"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    quality = _load_json(os.path.join(run_dir, "quality_report.json"))
    manifest = _load_json(os.path.join(run_dir, "manifest.json"))
    totals = quality.get("totals", {})
    table_metrics = next(iter(quality.get("tables", {}).values()), {})

    venta_total = float(df["venta_clp"].sum())
    costo_total = float(df["costo_clp"].sum())
    unidades_total = float(df["unidades"].sum())

    # ── Cobertura temporal ────────────────────────────────────────────────
    periodos = sorted(int(p) for p in df["periodo"].dropna().unique())
    anio_min, anio_max = periodos[0] // 100, periodos[-1] // 100
    esperados = [y * 100 + m for y in range(anio_min, anio_max + 1) for m in range(1, 13)]
    esperados = [p for p in esperados if periodos[0] <= p <= periodos[-1]]
    faltantes = [p for p in esperados if p not in set(periodos)]

    # ── Series y cortes ───────────────────────────────────────────────────
    mensual = _agg(df, "fecha").sort_values("fecha")
    mensual["periodo"] = mensual["fecha"].dt.strftime("%Y-%m")

    por_anio = _agg(df, "anio").sort_values("anio")
    por_anio["venta_yoy_pct"] = por_anio["venta"].pct_change() * 100
    por_anio["unidades_yoy_pct"] = por_anio["unidades"].pct_change() * 100

    por_grupo = _agg(df, "grupo_cliente")
    por_grupo["participacion_pct"] = por_grupo["venta"] / venta_total * 100

    nombres = _canonical_client_names(df)
    por_cliente = _agg(df, "cliente_codigo")
    por_cliente["cliente"] = por_cliente["cliente_codigo"].map(nombres)
    por_cliente["participacion_pct"] = por_cliente["venta"] / venta_total * 100

    por_region = _agg(df, "region")
    por_region["participacion_pct"] = por_region["venta"] / venta_total * 100

    por_comuna = _agg(df, "comuna")
    por_comuna["participacion_pct"] = por_comuna["venta"] / venta_total * 100

    por_categoria = _agg(df, "categoria")
    por_categoria["participacion_pct"] = por_categoria["venta"] / venta_total * 100

    por_familia = _agg(df, "familia")
    por_familia["participacion_pct"] = por_familia["venta"] / venta_total * 100

    por_producto = _agg(df, "producto")
    por_producto["participacion_pct"] = por_producto["venta"] / venta_total * 100
    por_producto["precio_unitario"] = np.where(
        por_producto["unidades"] != 0,
        por_producto["venta"] / por_producto["unidades"], np.nan
    )

    por_empaque = _agg(df, "empaque")
    por_empaque["participacion_pct"] = por_empaque["venta"] / venta_total * 100

    por_reposicion = _agg(df, "tipo_reposicion")
    por_reposicion["participacion_pct"] = por_reposicion["venta"] / venta_total * 100

    # Estacionalidad observada: promedio de venta por mes calendario,
    # expresado como índice sobre el promedio mensual global.
    por_mes = df.groupby("mes", observed=True)["venta_clp"].sum() / df.groupby("mes", observed=True)["anio"].nunique()
    indice_mes = (por_mes / por_mes.mean()).round(4)

    # Concentración (Pareto) de clientes
    ventas_cliente = por_cliente.sort_values("venta", ascending=False)["venta"]
    acumulado = ventas_cliente.cumsum() / venta_total
    clientes_80 = int((acumulado <= 0.80).sum() + 1)

    # ── Proyección de demanda 2023 ────────────────────────────────────────
    # El modelo no se elige a dedo: compite contra el último año observado y
    # gana el de menor MAPE (ver analytics/forecasting.py).
    serie_unidades = mensual.set_index("fecha")["unidades"]
    serie_venta = mensual.set_index("fecha")["venta"]
    fc_unidades = select_and_forecast(serie_unidades, horizon=12)
    # La venta se proyecta con el mismo modelo ganador de unidades para que
    # ambas cifras sean coherentes entre sí (misma forma, mismo supuesto).
    fc_venta = select_and_forecast(serie_venta, horizon=12,
                                   force_model=fc_unidades.best_model)

    proyeccion_mensual = []
    for ts, unid in fc_unidades.forecast.items():
        proyeccion_mensual.append({
            "periodo": ts.strftime("%Y-%m"),
            "mes": int(ts.month),
            "unidades_proy": round(float(unid)),
            "venta_proy": round(float(fc_venta.forecast.get(ts, np.nan)), 2),
        })

    # ── Plan de cotización por producto ───────────────────────────────────
    ultimo_anio = int(df["anio"].max())
    df_ult = df[df["anio"] == ultimo_anio]
    unidades_ult = df_ult.groupby("producto", observed=True)["unidades"].sum()
    # El último año tiene un mes ausente: se anualiza sobre meses observados
    # para no subestimar la base de la cotización.
    meses_observados = df_ult["periodo"].nunique()
    factor_anualizacion = 12 / meses_observados if meses_observados else 1.0

    total_hist = float(fc_unidades.history[fc_unidades.history.index.year == ultimo_anio].sum())
    total_proy = float(fc_unidades.forecast.sum())
    crecimiento = (total_proy / total_hist - 1) if total_hist else 0.0

    mensual_producto = df_ult.pivot_table(
        index="periodo", columns="producto", values="unidades",
        aggfunc="sum", observed=True
    ).fillna(0)

    plan = []
    for producto, unidades_anio in unidades_ult.sort_values(ascending=False).items():
        base = float(unidades_anio) * factor_anualizacion
        proyectado = base * (1 + crecimiento)
        mensual_p = mensual_producto[producto] if producto in mensual_producto else pd.Series(dtype=float)
        ss = safety_stock(mensual_p, SERVICE_LEVEL)
        plan.append({
            "producto": str(producto),
            "unidades_ultimo_anio": round(float(unidades_anio)),
            "base_anualizada": round(base),
            "unidades_proyectadas": round(proyectado),
            "pedido_mensual_promedio": round(proyectado / 12),
            # Colchón que se mantiene en bodega, no un extra que se compre cada
            # mes: se cotiza una vez y después sólo se repone lo consumido.
            "stock_seguridad": round(ss),
            "cotizacion_anual_sugerida": round(proyectado + ss),
            "participacion_pct": round(float(unidades_anio) / float(unidades_ult.sum()) * 100, 2),
        })

    # ── Portafolio vivo vs descontinuado ──────────────────────────────────
    # Un SKU que dejó de venderse no se cotiza. La lista sale de comparar el
    # surtido del último año contra el histórico, no de suponerlo.
    prod_ultimo = set(df_ult["producto"].dropna().unique())
    prod_previos = set(df[df["anio"] < ultimo_anio]["producto"].dropna().unique())
    ultima_venta = df.groupby("producto", observed=True)["periodo"].max()
    unidades_hist = df.groupby("producto", observed=True)["unidades"].sum()
    descontinuados = [
        {"producto": str(p),
         "ultima_venta": f"{int(ultima_venta[p])//100}-{int(ultima_venta[p])%100:02d}",
         "unidades_historicas": int(unidades_hist[p])}
        for p in sorted(prod_previos - prod_ultimo)
    ]
    nuevos = [
        {"producto": str(p), "unidades_ultimo_anio": int(unidades_ult.get(p, 0))}
        for p in sorted(prod_ultimo - prod_previos)
    ]

    # ── Calidad ───────────────────────────────────────────────────────────
    flags = table_metrics.get("flag_breakdown", {})
    margen_negativo_filas = int(flags.get("margen_negativo", 0))
    productos_margen_negativo = por_producto[por_producto["margen_pct"] < 0]

    insights = {
        "meta": {
            "generado": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "run_id": manifest.get("run_id", os.path.basename(run_dir)),
            "run_dir": run_dir,
            "archivo_origen": source_file or manifest.get("input", {}).get("path", ""),
            "filas_extraidas": int(totals.get("extracted", len(df))),
            "filas_limpias": int(totals.get("cleaned", len(df))),
            "filas_cuarentena": int(totals.get("quarantined", 0)),
            "ratio_cuarentena": float(totals.get("quarantine_ratio", 0.0)),
            "periodo_inicio": f"{periodos[0]//100}-{periodos[0]%100:02d}",
            "periodo_fin": f"{periodos[-1]//100}-{periodos[-1]%100:02d}",
            "meses_esperados": len(esperados),
            "meses_presentes": len(periodos),
            "meses_faltantes": [f"{p//100}-{p%100:02d}" for p in faltantes],
        },
        "kpis": {
            "venta_total": round(venta_total, 2),
            "costo_total": round(costo_total, 2),
            "margen_total": round(venta_total - costo_total, 2),
            "margen_pct": round((venta_total - costo_total) / venta_total * 100, 2),
            "unidades_total": int(unidades_total),
            "lineas": int(len(df)),
            "precio_promedio_unidad": round(venta_total / unidades_total, 2),
            "costo_promedio_unidad": round(costo_total / unidades_total, 2),
            "clientes": int(df["cliente_codigo"].nunique()),
            "grupos": int(df["grupo_cliente"].nunique()),
            "productos": int(df["producto"].nunique()),
            "familias": int(df["familia"].nunique()),
            "regiones": int(df["region"].nunique()),
            "comunas": int(df["comuna"].nunique()),
            "venta_promedio_mensual": round(float(mensual["venta"].mean()), 2),
            "unidades_promedio_mensual": round(float(mensual["unidades"].mean())),
        },
        "serie_mensual": _records(mensual, sort_by="fecha")[::-1],
        "por_anio": _records(por_anio, sort_by="anio")[::-1],
        "por_grupo": _records(por_grupo),
        "top_clientes": _records(por_cliente, top=TOP_N),
        "por_region": _records(por_region),
        "top_comunas": _records(por_comuna, top=TOP_N),
        "por_categoria": _records(por_categoria),
        "por_familia": _records(por_familia),
        "top_productos_venta": _records(por_producto, top=TOP_N),
        "top_productos_unidades": _records(por_producto, sort_by="unidades", top=TOP_N),
        "productos_todos": _records(por_producto),
        "por_empaque": _records(por_empaque),
        "por_reposicion": _records(por_reposicion),
        "estacionalidad": [
            {"mes": int(m), "indice": float(v),
             "venta_promedio": round(float(por_mes.loc[m]), 2)}
            for m, v in indice_mes.items()
        ],
        "portafolio": {
            "productos_activos_ultimo_anio": len(prod_ultimo),
            "productos_historicos": int(df["producto"].nunique()),
            "descontinuados": descontinuados,
            "nuevos_en_ultimo_anio": nuevos,
            "productos_por_anio": [
                {"anio": int(a), "productos": int(n)}
                for a, n in df.groupby("anio", observed=True)["producto"].nunique().items()
            ],
        },
        "concentracion": {
            "clientes_para_80pct_venta": clientes_80,
            "clientes_totales": int(len(por_cliente)),
            "pct_venta_top10_clientes": round(
                float(ventas_cliente.head(10).sum()) / venta_total * 100, 2),
            "pct_venta_top1_grupo": round(float(por_grupo["venta"].max()) / venta_total * 100, 2),
        },
        "proyeccion": {
            "modelo_elegido": fc_unidades.best_model,
            "metodo": MODEL_LABELS.get(fc_unidades.best_model, fc_unidades.best_model),
            "criterio_seleccion": (
                f"Menor MAPE prediciendo {ultimo_anio} sin haberlo visto "
                f"(entrenamiento hasta {ultimo_anio - 1}-12)"
            ),
            "comparacion_modelos": fc_unidades.comparison,
            "anio_proyectado": ultimo_anio + 1,
            "mensual": proyeccion_mensual,
            "unidades_total": round(total_proy),
            "venta_total": round(float(fc_venta.forecast.sum()), 2),
            "crecimiento_vs_ultimo_anio_pct": round(crecimiento * 100, 2),
            "mape_backtest_unidades": round(float(fc_unidades.mape_backtest), 2)
                if not np.isnan(fc_unidades.mape_backtest) else None,
            "mape_backtest_venta": round(float(fc_venta.mape_backtest), 2)
                if not np.isnan(fc_venta.mape_backtest) else None,
            "periodos_imputados": fc_unidades.imputed_periods,
            "indice_estacional": {str(k): round(v, 4) for k, v in fc_unidades.seasonal_index.items()},
            "serie_historica": [
                {"periodo": ts.strftime("%Y-%m"), "real": round(float(val))}
                for ts, val in fc_unidades.history.items()
            ],
            "backtest": [
                {"periodo": ts.strftime("%Y-%m"),
                 "real": round(float(fc_unidades.backtest_actual.loc[ts])),
                 "predicho": round(float(fc_unidades.backtest_pred.loc[ts]))}
                for ts in (fc_unidades.backtest_actual.index
                           if fc_unidades.backtest_pred is not None else [])
            ],
        },
        "plan_cotizacion": {
            "nivel_servicio": SERVICE_LEVEL,
            "anio_base": ultimo_anio,
            "meses_observados_anio_base": int(meses_observados),
            "factor_anualizacion": round(factor_anualizacion, 4),
            "criterio": (
                f"Base = unidades {ultimo_anio} anualizadas × "
                f"(1 + crecimiento proyectado); colchón = z·σ de la demanda "
                f"mensual al {SERVICE_LEVEL*100:.0f}% de nivel de servicio"
            ),
            "items": plan,
            "total_unidades_proyectadas": round(sum(p["unidades_proyectadas"] for p in plan)),
            "total_stock_seguridad": round(sum(p["stock_seguridad"] for p in plan)),
            "total_unidades_a_cotizar": round(sum(p["cotizacion_anual_sugerida"] for p in plan)),
        },
        "calidad": {
            "flags": flags,
            "violaciones_reglas": table_metrics.get("rule_violations", {}),
            "score_distribucion": table_metrics.get("quality_score_distribution", {}),
            "filas_margen_negativo": margen_negativo_filas,
            "pct_filas_margen_negativo": round(margen_negativo_filas / len(df) * 100, 2),
            "productos_margen_negativo": _records(productos_margen_negativo, sort_by="venta"),
            "clientes_nombre_inconsistente": int(
                df.dropna(subset=["cliente_codigo"])
                .groupby("cliente_codigo", observed=True)["cliente_nombre"]
                .nunique().gt(1).sum()
            ),
        },
    }
    return insights


# ────────────────────────────── CLI ───────────────────────────────────────
def main() -> dict:
    parser = argparse.ArgumentParser(description="Insights caso Abarrotes CL")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--json-out", default=None,
                        help="Ruta del JSON de insights (default: <run_dir>/insights.json)")
    args = parser.parse_args()

    run_dir = resolve_run_dir(args.output_root, args.run_dir)
    df = load_clean_dataset(run_dir)
    insights = build_insights(df, run_dir=run_dir)

    json_out = args.json_out or os.path.join(run_dir, "insights.json")
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(insights, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f"Insights escritos en {json_out}")
    print(json.dumps(insights["kpis"], indent=2, ensure_ascii=False))
    return insights


if __name__ == "__main__":
    main()
