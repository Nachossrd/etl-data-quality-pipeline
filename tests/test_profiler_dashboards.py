"""Tests del perfilado automático, el export SQL y los tableros genéricos.

El perfilador decide qué se grafica: si clasifica mal una columna, el tablero
completo miente. Estos tests fijan los casos donde la intuición ingenua falla
—subcadenas engañosas, decimales continuos, columnas derivadas del propio
pipeline— porque todos ellos ya rompieron el tablero una vez.
"""

import json
import os
import sqlite3

import numpy as np
import pandas as pd
import pytest

from analytics import portal
from analytics.generic_dashboard import build_insights
from analytics.profiler import profile_dataset
from core.sql_export import export_run


@pytest.fixture
def ventas() -> pd.DataFrame:
    rng = np.random.default_rng(3)
    n = 600
    fechas = pd.to_datetime("2023-01-01") + pd.to_timedelta(rng.integers(0, 700, n), "D")
    return pd.DataFrame({
        "orden_id": [f"ORD-{i:05d}" for i in range(n)],
        "fecha": fechas,
        "canal": rng.choice(["Web", "Tienda", "App"], n),
        "ciudad": rng.choice(["Santiago", "Valparaiso", "Temuco", "Arica"], n),
        "unidades": rng.integers(1, 30, n),
        "monto": np.round(rng.gamma(4, 9000, n), 2),
        "anio": fechas.year,
    })


# ─── Roles de columna ──────────────────────────────────────────────────────
def test_roles_basicos(ventas):
    p = profile_dataset(ventas, "ventas")
    roles = {c.name: c.role for c in p.columns}
    assert roles["fecha"] == "date"
    assert roles["monto"] == "measure"
    assert roles["canal"] == "dimension"
    assert roles["orden_id"] == "identifier"
    assert p.primary_date == "fecha"


def test_unidades_no_es_llave_por_contener_id(ventas):
    """'unidades' contiene la subcadena 'id': con comparación ingenua
    terminaba clasificada como llave y desaparecía de las medidas."""
    p = profile_dataset(ventas, "ventas")
    assert "unidades" in p.measures
    assert "unidades" not in p.identifiers


def test_trimestre_no_es_fecha_por_contener_mes():
    """'trimestre' contiene 'mes'; sin comparación por palabra completa se
    clasificaba como columna de fecha."""
    df = pd.DataFrame({"trimestre": ["T1", "T2", "T3", "T4"] * 5,
                       "valor": range(20)})
    roles = {c.name: c.role for c in profile_dataset(df, "t").columns}
    assert roles["trimestre"] == "dimension"


def test_decimal_continuo_es_medida_no_llave():
    """Un monto tiene casi todos sus valores distintos, pero nadie usa
    1.234,56 como llave: si se clasifica como identificador, el tablero
    elige otra columna como medida principal."""
    df = pd.DataFrame({"monto": np.linspace(1000.5, 99999.5, 500),
                       "cat": ["a", "b"] * 250})
    p = profile_dataset(df, "t")
    assert p.measures == ["monto"]
    assert not p.identifiers


def test_codigo_de_baja_cardinalidad_es_dimension():
    """`region_codigo` con 16 valores es categoría aunque se llame código:
    manda la cardinalidad, no el nombre."""
    df = pd.DataFrame({"region_codigo": [f"R{i%16}" for i in range(500)],
                       "cliente_codigo": [f"C{i:04d}" for i in range(500)],
                       "v": range(500)})
    p = profile_dataset(df, "t")
    assert "region_codigo" in p.dimensions
    assert "cliente_codigo" in p.identifiers


def test_numerico_temporal_no_se_suma():
    """Sumar la columna `periodo` (202101) no significa nada."""
    df = pd.DataFrame({"periodo": [202101 + i for i in range(40)] * 3,
                       "monto": np.arange(120.0)})
    p = profile_dataset(df, "t")
    assert "periodo" not in p.measures


def test_columnas_derivadas_del_pipeline_quedan_fuera():
    """El motor semántico deja `monto_parsed`, `monto_final`, `monto_original`…
    Son la pista de auditoría, no cinco medidas distintas."""
    df = pd.DataFrame({
        "monto": np.arange(100.0), "monto_parsed": np.arange(100.0),
        "monto_final": np.arange(100.0), "monto_original": np.arange(100.0),
        "monto_monto_origen": np.arange(100.0), "quality_score": [100] * 100,
        "data_quality_flags": [[] for _ in range(100)],
    })
    p = profile_dataset(df, "t")
    assert p.measures == ["monto"]


def test_constante_se_detecta():
    df = pd.DataFrame({"pais": ["CL"] * 50, "v": range(50)})
    roles = {c.name: c.role for c in profile_dataset(df, "t").columns}
    assert roles["pais"] == "constant"


def test_dataset_vacio_no_revienta():
    p = profile_dataset(pd.DataFrame({"a": []}), "vacio")
    assert p.rows == 0 and p.measures == []


# ─── Insights genéricos ────────────────────────────────────────────────────
def test_insights_arma_serie_rankings_y_distribucion(ventas):
    ins = build_insights(ventas, "ventas")
    assert ins["serie"]["columna"] == "fecha"
    assert ins["serie"]["granularidad"] == "month"
    assert len(ins["serie"]["puntos"]) > 12
    assert {d["columna"] for d in ins["dimensiones"]} >= {"canal", "ciudad"}
    assert ins["distribucion"]["columna"] == "monto"
    assert ins["medidas"][0]["columna"] == "monto"     # la de mayor magnitud


def test_los_rankings_suman_el_total(ventas):
    ins = build_insights(ventas, "ventas")
    canal = next(d for d in ins["dimensiones"] if d["columna"] == "canal")
    assert canal["total"] == pytest.approx(ventas["monto"].sum(), rel=1e-6)
    assert sum(i["valor"] for i in canal["items"]) == pytest.approx(
        canal["total"], rel=1e-6)


def test_dataset_sin_fecha_ni_medida_igual_produce_tablero():
    """Un catálogo puro: sin serie ni distribución, pero con rankings."""
    df = pd.DataFrame({"sku": [f"S{i}" for i in range(60)],
                       "familia": ["A", "B", "C"] * 20})
    ins = build_insights(df, "catalogo")
    assert ins["serie"] is None
    assert ins["distribucion"] is None
    assert ins["dimensiones"][0]["metrica"] == "conteo"
    assert ins["meta"]["filas"] == 60


def test_dimensiones_redundantes_se_descartan():
    """`mes` y `mes_nombre` son la misma columna: graficar ambas repite."""
    meses = list(range(1, 13)) * 10
    nombres = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"]
    df = pd.DataFrame({"mes": meses, "mes_nombre": [nombres[m-1] for m in meses],
                       "monto": np.arange(120.0)})
    graficadas = [d["columna"] for d in build_insights(df, "t")["dimensiones"]]
    assert not ({"mes", "mes_nombre"} <= set(graficadas))


def test_granularidad_diaria_para_rangos_cortos():
    df = pd.DataFrame({
        "fecha": pd.date_range("2024-01-01", periods=45, freq="D"),
        "monto": np.arange(45.0),
    })
    assert build_insights(df, "t")["serie"]["granularidad"] == "day"


# ─── Export SQL ────────────────────────────────────────────────────────────
def test_export_run_genera_duckdb_y_sqlite(tmp_path, ventas):
    run = tmp_path / "run1"
    run.mkdir()
    ventas.to_parquet(run / "ventas_clean.parquet")
    result = export_run(str(run))

    assert result["tables"] == ["ventas"]
    assert os.path.exists(result["sqlite"])
    con = sqlite3.connect(result["sqlite"])
    assert con.execute("SELECT COUNT(*) FROM ventas").fetchone()[0] == len(ventas)
    con.close()

    duckdb = pytest.importorskip("duckdb")
    d = duckdb.connect(result["duckdb"])
    assert d.execute("SELECT COUNT(*) FROM ventas").fetchone()[0] == len(ventas)
    d.close()


def test_sqlite_serializa_las_listas(tmp_path):
    """SQLite no tiene tipo lista: `data_quality_flags` debe ir como texto."""
    run = tmp_path / "run2"
    run.mkdir()
    pd.DataFrame({"a": [1, 2],
                  "data_quality_flags": [["x", "y"], []]}).to_parquet(
        run / "t_clean.parquet")
    result = export_run(str(run))
    con = sqlite3.connect(result["sqlite"])
    valores = [r[0] for r in con.execute(
        "SELECT data_quality_flags FROM t ORDER BY a").fetchall()]
    con.close()
    assert valores == ["x;y", ""]


def test_tabla_ancha_no_revienta_el_limite_de_variables(tmp_path):
    """SQLite topea en 32.766 variables por sentencia; con 60 columnas el
    lote hay que achicarlo o falla con 'too many SQL variables'."""
    run = tmp_path / "run3"
    run.mkdir()
    df = pd.DataFrame({f"c{i}": np.arange(2000) for i in range(60)})
    df.to_parquet(run / "ancha_clean.parquet")
    result = export_run(str(run))
    con = sqlite3.connect(result["sqlite"])
    assert con.execute("SELECT COUNT(*) FROM ancha").fetchone()[0] == 2000
    con.close()


def test_run_sin_tablas_no_falla(tmp_path):
    run = tmp_path / "vacio"
    run.mkdir()
    assert export_run(str(run))["tables"] == []


# ─── Portada ───────────────────────────────────────────────────────────────
def test_slugify():
    assert portal.slugify("Ventas 2019-al-2022") == "ventas-2019-al-2022"
    assert portal.slugify("Año Ñandú") == "ano-nandu"
    assert portal.slugify("///") == "dataset"


def test_portada_lista_los_datasets_publicados(tmp_path):
    for nombre, filas in (("uno", 10), ("dos", 20)):
        d = tmp_path / nombre
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({
            "tabla": nombre, "titulo": nombre, "tipo": "generico",
            "filas": filas, "columnas": 3, "generado": f"2026-01-0{filas//10}",
        }), encoding="utf-8")
    (tmp_path / "sin_meta").mkdir()

    datasets = portal.collect(str(tmp_path))
    assert [d["tabla"] for d in datasets] == ["dos", "uno"]   # más reciente primero

    path = portal.build(str(tmp_path))
    html = open(path, encoding="utf-8").read()
    assert "uno" in html and "dos" in html
    assert html.startswith("<!doctype html>")


def test_portada_vacia_no_falla(tmp_path):
    path = portal.build(str(tmp_path))
    assert os.path.exists(path)
