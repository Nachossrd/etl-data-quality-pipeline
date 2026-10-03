#!/usr/bin/env python
"""Data Clean de punta a punta: un archivo entra, un tablero sale.

    python run_data_clean.py --input ventas.xlsx
    python run_data_clean.py --input carpeta_con_datos/
    python run_data_clean.py --input export.zip

Encadena las cuatro etapas:

    1. ETL        cualquier formato -> tabla(s) limpia(s) + auditoría de calidad
    2. SQL        dataset.duckdb + dataset.db (sin servidor, listos para consultar)
    3. Tableros   uno por tabla: especializado si existe, auto-perfilado si no
    4. Portada    índice de todos los datasets publicados

Formatos de entrada: csv, tsv, txt, xlsx, xls, json, jsonl, parquet, sqlite,
db, sql, bak, zip y carpetas completas.

Salidas de cada run en `output/<run_id>/`; los tableros se publican en la
carpeta indicada por `--publish` (por defecto, El cubo).
"""

import argparse
import json
import os
import sys
import time
from typing import List

DEFAULT_OUTPUT = "./output"
DEFAULT_PUBLISH = r"C:\Users\marku\Downloads\El cubo\data-clean"


def parse_args():
    p = argparse.ArgumentParser(
        description="ETL + SQL + tableros automáticos para cualquier archivo")
    p.add_argument("--input", required=False,
                   help="Archivo o carpeta a procesar")
    p.add_argument("--output", default=DEFAULT_OUTPUT,
                   help="Directorio de runs (default ./output)")
    p.add_argument("--publish", default=DEFAULT_PUBLISH,
                   help="Carpeta donde se publican los tableros")
    p.add_argument("--run-dir", default=None,
                   help="Reusar un run existente en vez de procesar de nuevo")
    p.add_argument("--quarantine-threshold", type=float, default=None)
    p.add_argument("--no-sql", action="store_true", help="Omitir DuckDB/SQLite")
    p.add_argument("--no-publish", action="store_true", help="No generar tableros")
    p.add_argument("--formats", action="store_true",
                   help="Listar formatos soportados y salir")
    return p.parse_args()


def _step(n: int, title: str) -> None:
    print(f"\n{'='*68}\n  {n}. {title}\n{'='*68}", flush=True)


def _human(path: str) -> str:
    try:
        size = os.path.getsize(path)
    except OSError:
        return path
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{path}  ({size:.0f} {unit})"
        size /= 1024
    return f"{path}  ({size:.1f} TB)"


def main() -> int:
    args = parse_args()

    if args.formats:
        from core.ingest import describe_support
        print("Formatos soportados:\n")
        for familia, exts in describe_support().items():
            print(f"  {familia:<20} {', '.join(exts)}")
        return 0

    if not args.input and not args.run_dir:
        print("[ERROR] Indica --input <archivo|carpeta> o --run-dir <run existente>")
        return 1

    started = time.time()
    import pandas as pd

    from analytics import portal, registry
    from core.logging_engine import setup_logger
    from core.sql_export import export_run

    logger = setup_logger("run_data_clean")

    # ── 1. ETL ────────────────────────────────────────────────────────────
    if args.run_dir:
        run_dir = args.run_dir
        _step(1, f"ETL omitido — reusando {run_dir}")
    else:
        _step(1, f"ETL sobre {args.input}")
        if not os.path.exists(args.input):
            print(f"[ERROR] No existe: {args.input}")
            return 1
        from auto_pipeline import run_pipeline
        result = run_pipeline(args.input, args.output,
                              quarantine_threshold=args.quarantine_threshold)
        run_dir = result["run_dir"]
        if result["quarantine_failed"]:
            # Publicar un tablero sobre datos que el propio pipeline declaró
            # dudosos sería peor que no publicar nada.
            print(f"\n[ERROR] Cuarentena {result['q_ratio']*100:.2f}% sobre el umbral. "
                  f"No se publica el tablero. Revisar {run_dir}/quarantine/")
            return 2

    # Tablas limpias del run
    tablas: List[str] = sorted(
        f[: -len("_clean.parquet")] for f in os.listdir(run_dir)
        if f.endswith("_clean.parquet")
    )
    if not tablas:
        print(f"[ERROR] El run {run_dir} no produjo tablas limpias")
        return 1
    print(f"  Tablas limpias: {', '.join(tablas)}")

    quality = {}
    qpath = os.path.join(run_dir, "quality_report.json")
    if os.path.exists(qpath):
        with open(qpath, encoding="utf-8") as f:
            quality = json.load(f)

    # Con --run-dir no hay --input: el nombre del archivo original lo sabe el
    # manifest del run, y es lo que debe mostrarse como origen en la portada.
    origen = os.path.basename(args.input) if args.input else ""
    if not origen:
        mpath = os.path.join(run_dir, "manifest.json")
        if os.path.exists(mpath):
            with open(mpath, encoding="utf-8") as f:
                inputs = json.load(f).get("input_files") or []
            if inputs:
                origen = os.path.basename(inputs[0].get("path", ""))
    origen = origen or os.path.basename(run_dir)

    # ── 2. SQL local ──────────────────────────────────────────────────────
    if not args.no_sql:
        _step(2, "Bases SQL locales (sin servidor)")
        sql = export_run(run_dir)
        for label, key in (("DuckDB", "duckdb"), ("SQLite", "sqlite"),
                           ("Consultas", "query_guide")):
            if sql.get(key):
                print(f"  {label:<10} {_human(sql[key])}")

    # ── 3. Tableros ───────────────────────────────────────────────────────
    if args.no_publish:
        print(f"\nListo en {time.time() - started:.1f}s — publicación omitida")
        return 0

    _step(3, "Tableros")
    publicados = []
    for tabla in tablas:
        parquet = os.path.join(run_dir, f"{tabla}_clean.parquet")
        try:
            df = pd.read_parquet(parquet)
        except Exception as e:
            logger.error(f"No se pudo leer '{parquet}': {e}")
            continue
        if df.empty:
            logger.warning(f"Tabla '{tabla}' vacía; sin tablero")
            continue
        out_dir = os.path.join(args.publish, portal.slugify(tabla))
        try:
            meta = registry.build(df, tabla, out_dir, run_dir=run_dir,
                                  quality=quality, origen=origen)
            publicados.append(meta)
            print(f"  {tabla:<28} {meta['tipo']:<12} "
                  f"{os.path.join(out_dir, 'index.html')}")
        except Exception as e:
            logger.error(f"Tablero de '{tabla}' falló: {e}")

    # ── 4. Portada ────────────────────────────────────────────────────────
    _step(4, "Portada")
    index = portal.build(args.publish)
    print(f"  {index}")
    print(f"\n{len(publicados)} tablero(s) publicado(s) · listo en "
          f"{time.time() - started:.1f}s")
    print(f"\nAbrir: {index}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
