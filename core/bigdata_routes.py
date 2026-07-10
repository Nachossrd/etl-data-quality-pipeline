"""
Rutas HTTP del panel "Preparar dataset multi-tabla" de Data clean.

Data clean es una herramienta de PREPARACIÓN, no de consulta.
Este módulo expone una única acción: dada una carpeta con archivos crudos
(TSV/CSV), genera la versión limpia en Parquet lista para ser consultada
desde cualquier herramienta SQL externa (DBeaver, DuckDB, Spark, Pandas).

Endpoint:
    POST /bigdata/prepare-dataset
        body: {"source": "ruta/a/carpeta", "output": "ruta/salida"}
        out:  {"success": bool, "stats": {tabla: n_filas}, "output": ruta}

Para consultar después se usan herramientas externas — no Data clean.
"""

import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from core.logging_engine import setup_logger

logger = setup_logger("bigdata_routes")


# ─── SparkSession singleton (lazy + thread-safe) ──────────────────────────────
_spark_lock = threading.Lock()
_spark = None


def _get_spark():
    global _spark
    with _spark_lock:
        if _spark is None:
            from pyspark.sql import SparkSession
            _spark = (
                SparkSession.builder
                .appName("DataClean_DatasetPreparer")
                .master("local[*]")
                .config("spark.driver.memory", "6g")
                .config("spark.sql.shuffle.partitions", "16")
                .getOrCreate()
            )
            _spark.sparkContext.setLogLevel("WARN")
            logger.info("SparkSession iniciada para preparación")
        return _spark


# ─── Handler ──────────────────────────────────────────────────────────────────
def handle_prepare_dataset(body: bytes) -> Dict[str, Any]:
    """Limpia todos los TSV/CSV reconocidos en `source` y los exporta a Parquet.

    Reconoce automáticamente los archivos IMDb estándar (title.basics, etc.)
    y aplica el schema oficial. Otros TSV/CSV pueden añadirse extendiendo
    `core.imdb_dataset.FILE_TO_VIEW`.
    """
    from core.imdb_dataset import ImdbDataset, FILE_TO_VIEW

    params = json.loads(body) if body else {}
    base = Path(__file__).resolve().parent.parent / "ultimo trabajo"
    source = Path(params.get("source") or base / "imdb_data").resolve()
    output = Path(params.get("output") or base / "imdb_parquet").resolve()

    if not source.is_dir():
        return {"success": False, "error": f"No existe carpeta source: {source}"}

    found = [f for f in FILE_TO_VIEW if (source / f).exists()]
    if not found:
        return {
            "success": False,
            "error": f"No se encontraron archivos reconocidos en {source}. "
                     f"Esperados (alguno de): {list(FILE_TO_VIEW)}",
        }

    spark = _get_spark()
    stats = ImdbDataset.prepare_all(spark, source, output)

    return {
        "success": True,
        "stats": stats,
        "output": str(output),
        "files_processed": found,
        "how_to_query": {
            "duckdb": f"SELECT * FROM read_parquet('{output}/title_basics/*.parquet') LIMIT 10",
            "spark": "spark.read.parquet(r'" + str(output) + "/title_basics').createOrReplaceTempView('title_basics')",
            "pandas": "import pandas as pd; pd.read_parquet(r'" + str(output) + "/title_basics')",
        },
    }


# ─── Dispatcher ───────────────────────────────────────────────────────────────
ROUTES = {
    "/bigdata/prepare-dataset": handle_prepare_dataset,
}


def dispatch(path: str, body: bytes) -> Optional[Dict[str, Any]]:
    handler = ROUTES.get(path)
    if handler is None:
        return None
    try:
        return handler(body)
    except Exception as e:
        logger.exception(f"Error en {path}")
        return {"success": False, "error": str(e)}
