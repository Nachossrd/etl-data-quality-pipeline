"""Configuración centralizada del pipeline.

Carga `.env` al importar (vía python-dotenv) para que las credenciales y
toggles vivan fuera del repo. Los defaults son solo para desarrollo local;
producción debe sobrescribir via `.env`.

Convención:
    SQL_*       credenciales para Docker SQL Server efímero y AnalyticsDB
    PIPELINE_*  configuración runtime del pipeline
"""

import os
from pathlib import Path

# Carga .env si existe. No falla si no está (compatible con CI sin secretos).
try:
    from dotenv import load_dotenv
    _env_file = Path(__file__).resolve().parent.parent / ".env"
    if _env_file.exists():
        load_dotenv(_env_file, override=False)
except ImportError:
    pass


class PipelineConfig:
    # ─── Credenciales SQL (Docker efímero + AnalyticsDB) ──────────────────
    # En producción: sobrescribir vía .env. Default solo dev local.
    SQL_SA_PASSWORD: str = os.environ.get("SQL_SA_PASSWORD", "SuperSecurePass123!")
    SQL_PORT: int = int(os.environ.get("SQL_PORT", "1433"))
    SQL_HOST: str = os.environ.get("SQL_HOST", "localhost")
    SQL_DATABASE: str = os.environ.get("SQL_DATABASE", "AnalyticsDB")
    SQL_DRIVER: str = os.environ.get("SQL_DRIVER", "ODBC Driver 17 for SQL Server")

    # ─── Docker ───────────────────────────────────────────────────────────
    DOCKER_MEMORY: str = os.environ.get("DOCKER_MEMORY", "4g")

    # ─── Extracción ───────────────────────────────────────────────────────
    CHUNK_SIZE: int = int(os.environ.get("PIPELINE_CHUNK_SIZE", "50000"))

    # ─── Paths ────────────────────────────────────────────────────────────
    OUTPUT_DIR: str = os.environ.get("PIPELINE_OUTPUT_DIR", "./output")
    QUARANTINE_DIR: str = os.environ.get("PIPELINE_QUARANTINE_DIR", "./quarantine")
    LOGS_DIR: str = os.environ.get("PIPELINE_LOGS_DIR", "./logs")

    # ─── Thresholds ───────────────────────────────────────────────────────
    CONVERSION_THRESHOLD: float = float(
        os.environ.get("PIPELINE_CONVERSION_THRESHOLD", "0.85"))
    DETECTION_THRESHOLD: float = float(
        os.environ.get("PIPELINE_DETECTION_THRESHOLD", "0.60"))
    # Si más de N% de filas terminan en cuarentena, el pipeline falla con
    # exit code != 0 en lugar de reportar éxito con datos sospechosos.
    QUARANTINE_FAIL_THRESHOLD: float = float(
        os.environ.get("PIPELINE_QUARANTINE_FAIL_THRESHOLD", "0.05"))

    # ─── Excel export ─────────────────────────────────────────────────────
    EXCEL_ROWS_PER_SHEET: int = int(
        os.environ.get("PIPELINE_EXCEL_ROWS_PER_SHEET", "1000000"))

    # ─── Destinos SQL ─────────────────────────────────────────────────────
    # Las bases consultables del run (DuckDB + SQLite) se generan siempre y no
    # necesitan servidor. El export a SQL Server es opt-in: sin instancia
    # levantada sólo produce timeouts de ODBC y ruido en el log.
    SQL_SERVER_EXPORT: bool = os.environ.get(
        "PIPELINE_SQL_SERVER_EXPORT", "0").strip().lower() in ("1", "true", "yes")


def get_sql_connection_string(database: str = None) -> str:
    """Construye la URL ODBC para SQLAlchemy. Centraliza el patrón."""
    db = database or PipelineConfig.SQL_DATABASE
    driver = PipelineConfig.SQL_DRIVER.replace(" ", "+")
    return (
        f"mssql+pyodbc://sa:{PipelineConfig.SQL_SA_PASSWORD}"
        f"@{PipelineConfig.SQL_HOST}:{PipelineConfig.SQL_PORT}/{db}"
        f"?driver={driver}"
    )
