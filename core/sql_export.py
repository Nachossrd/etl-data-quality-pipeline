"""Exporta el resultado del run a bases SQL locales, sin servidor.

Antes, la única salida SQL del pipeline era SQL Server (`AnalyticsDB`), que
exige un servidor levantado: si no está, el run "termina bien" pero deja un
error en el log y ninguna base consultable. Aquí se generan dos archivos que
no dependen de nada:

    dataset.duckdb   analítica: agregaciones sobre millones de filas en ms
    dataset.db       SQLite: lo abre cualquier herramienta, hasta Excel vía ODBC

Ambos contienen **todas las tablas del run**, con los nombres ya saneados para
SQL. El Parquet y el CSV siguen ahí para Power BI / Tableau.

Diferencia de tipos entre motores: DuckDB conserva las listas nativas
(`data_quality_flags` sigue siendo una lista consultable con `list_contains`);
SQLite no tiene tipo lista, así que ahí se serializan como `tag1;tag2`.
"""

import os
import sqlite3
from typing import Dict, List, Optional

import pandas as pd

from core.logging_engine import setup_logger

logger = setup_logger("sql_export")

DUCKDB_FILE = "dataset.duckdb"
SQLITE_FILE = "dataset.db"


def _clean_tables(run_dir: str) -> Dict[str, str]:
    """Mapa nombre_tabla -> ruta del archivo limpio (Parquet si existe)."""
    tables: Dict[str, str] = {}
    for fname in sorted(os.listdir(run_dir)):
        path = os.path.join(run_dir, fname)
        if fname.endswith("_clean.parquet"):
            tables[fname[: -len("_clean.parquet")]] = path
        elif fname.endswith("_clean.csv"):
            name = fname[: -len("_clean.csv")]
            tables.setdefault(name, path)      # el Parquet manda si están ambos
    return tables


def _flatten_for_sqlite(df: pd.DataFrame) -> pd.DataFrame:
    """SQLite sólo acepta escalares: listas y dicts se serializan a texto.

    Ojo con el tipo: al volver de Parquet, una columna de listas llega como
    `numpy.ndarray`, no como `list`. Sin contemplarlo, el driver la guardaba
    como BLOB con los bytes crudos del buffer — datos corruptos que sólo se
    notan al consultarlos.
    """
    import numpy as np

    seq = (list, tuple, np.ndarray)
    df = df.copy()
    for col in df.columns:
        sample = df[col].dropna()
        if sample.empty:
            continue
        first = sample.iloc[0]
        if isinstance(first, seq):
            df[col] = df[col].apply(
                lambda v: ";".join(map(str, v)) if isinstance(v, seq)
                else ("" if v is None else str(v))
            )
        elif isinstance(first, dict):
            df[col] = df[col].astype(str)
    return df


def export_duckdb(run_dir: str, tables: Dict[str, str]) -> Optional[str]:
    try:
        import duckdb
    except ImportError:
        logger.warning("duckdb no instalado; se omite dataset.duckdb")
        return None

    path = os.path.join(run_dir, DUCKDB_FILE)
    if os.path.exists(path):
        os.remove(path)                       # el run es inmutable: se regenera

    con = duckdb.connect(path)
    try:
        for name, src in tables.items():
            quoted = name.replace('"', '""')
            reader = "read_parquet" if src.endswith(".parquet") else "read_csv_auto"
            con.execute(
                f'CREATE OR REPLACE TABLE "{quoted}" AS '
                f"SELECT * FROM {reader}('{src.replace(chr(39), chr(39)*2)}')"
            )
            rows = con.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]
            logger.info(f"DuckDB: tabla '{name}' con {rows:,} filas")
    finally:
        con.close()
    return path


def export_sqlite(run_dir: str, tables: Dict[str, str],
                  chunk_size: int = 50_000) -> str:
    path = os.path.join(run_dir, SQLITE_FILE)
    if os.path.exists(path):
        os.remove(path)

    con = sqlite3.connect(path)
    try:
        for name, src in tables.items():
            if src.endswith(".parquet"):
                frames = [pd.read_parquet(src)]
            else:
                frames = pd.read_csv(src, chunksize=chunk_size, low_memory=False)

            first = True
            total = 0
            for frame in frames:
                # SQLite topea en 32.766 variables por sentencia; con inserción
                # multi-fila el lote son filas × columnas. Sin este ajuste, una
                # tabla ancha revienta con "too many SQL variables".
                safe_rows = max(1, 30_000 // max(1, len(frame.columns)))
                _flatten_for_sqlite(frame).to_sql(
                    name, con, if_exists="replace" if first else "append",
                    index=False, chunksize=min(chunk_size, safe_rows),
                    method="multi",
                )
                total += len(frame)
                first = False
            logger.info(f"SQLite: tabla '{name}' con {total:,} filas")
        con.commit()
    finally:
        con.close()
    return path


def write_query_guide(run_dir: str, tables: Dict[str, str]) -> str:
    """Deja un .sql con ejemplos listos para copiar y pegar."""
    first = next(iter(tables), "tabla")
    lines = [
        "-- Consultas de ejemplo sobre el run.",
        "-- DuckDB:  duckdb dataset.duckdb",
        "-- SQLite:  sqlite3 dataset.db",
        "",
        f"-- Tablas disponibles: {', '.join(tables) or '(ninguna)'}",
        "",
        f'SELECT * FROM "{first}" LIMIT 20;',
        "",
        f'SELECT COUNT(*) AS filas FROM "{first}";',
        "",
        "-- DuckDB puede consultar el Parquet sin importarlo:",
        f"-- SELECT * FROM '{first}_clean.parquet' LIMIT 20;",
        "",
        "-- Filas marcadas por el control de calidad (DuckDB, lista nativa):",
        f"-- SELECT * FROM \"{first}\" WHERE len(data_quality_flags) > 0;",
        "-- En SQLite las mismas filas:",
        f"-- SELECT * FROM \"{first}\" WHERE data_quality_flags <> '';",
    ]
    path = os.path.join(run_dir, "consultas.sql")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def export_run(run_dir: str) -> Dict[str, object]:
    """Genera las bases SQL del run. Devuelve rutas y tablas exportadas."""
    tables = _clean_tables(run_dir)
    if not tables:
        logger.warning(f"El run {run_dir} no tiene tablas limpias que exportar")
        return {"tables": [], "duckdb": None, "sqlite": None}

    logger.info(f"Exportando {len(tables)} tabla(s) a SQL local: "
                f"{', '.join(tables)}")
    result: Dict[str, object] = {"tables": list(tables)}
    try:
        result["duckdb"] = export_duckdb(run_dir, tables)
    except Exception as e:
        logger.error(f"Export a DuckDB falló: {e}")
        result["duckdb"] = None
    try:
        result["sqlite"] = export_sqlite(run_dir, tables)
    except Exception as e:
        logger.error(f"Export a SQLite falló: {e}")
        result["sqlite"] = None
    result["query_guide"] = write_query_guide(run_dir, tables)
    return result
