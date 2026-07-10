"""Cleaners pipeline-friendly para datasets IMDb (pandas, chunked).

Para datasets grandes (`title.principals2.tsv` 3.5GB), usar la pipeline
PySpark de `core/imdb_dataset.py` que es más eficiente. Estos cleaners
existen para que datasets IMDb pequeños (ratings 25MB) o samples puedan
ir por el pipeline pandas estándar y beneficiarse de quality_report,
manifest y rules declarativas.

Reglas de limpieza:
    - reemplazar "\\N" por NA (marcador IMDb estándar)
    - cast de tipos según schema oficial
    - filtrar filas sin clave primaria (tconst/nconst)

Detección: por columnas únicas a cada tabla IMDb.
"""

import pandas as pd

from core.cleaners import Cleaner
from core.logging_engine import setup_logger

logger = setup_logger("imdb_cleaners")

_NULL_MARKER = "\\N"


def _replace_imdb_nulls(df: pd.DataFrame) -> pd.DataFrame:
    """Convierte el marcador \\N de IMDb a NA en todas las columnas string."""
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].replace(_NULL_MARKER, pd.NA)
    return df


def _cast_int(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    return df


def _cast_float(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Float64")
    return df


class ImdbTitleBasicsCleaner(Cleaner):
    name = "imdb_title_basics"

    _SIGNATURE = {"tconst", "titleType", "primaryTitle", "startYear"}

    @staticmethod
    def matches(df: pd.DataFrame) -> bool:
        return ImdbTitleBasicsCleaner._SIGNATURE.issubset(set(df.columns))

    @staticmethod
    def clean_chunk(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = _replace_imdb_nulls(df)
        for c in ("startYear", "endYear", "runtimeMinutes"):
            df = _cast_int(df, c)
        if "tconst" in df.columns:
            df = df[df["tconst"].notna()].reset_index(drop=True)
        return df


class ImdbTitleRatingsCleaner(Cleaner):
    name = "imdb_title_ratings"

    _SIGNATURE = {"tconst", "averageRating", "numVotes"}

    @staticmethod
    def matches(df: pd.DataFrame) -> bool:
        return ImdbTitleRatingsCleaner._SIGNATURE.issubset(set(df.columns))

    @staticmethod
    def clean_chunk(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = _replace_imdb_nulls(df)
        df = _cast_float(df, "averageRating")
        df = _cast_int(df, "numVotes")
        if "tconst" in df.columns:
            df = df[df["tconst"].notna()].reset_index(drop=True)
        return df
