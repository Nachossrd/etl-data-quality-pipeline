"""Cleaner para datos transaccionales en castellano (legacy CleaningEngine).

Mueve la lógica de `core/cleaning_engine.py` original a un Cleaner registrable.
Aplica detección por nombres de columna en español: IDs (TRX-XXXX), fechas
("fecha"/"date"), montos ("monto"/"precio"/"total"), estados ("estado"/"status"),
hardware ("hardware"/"sku"/"producto"). Es el fallback por defecto.
"""

import re

import pandas as pd

from core.cleaners import Cleaner
from core.logging_engine import setup_logger
from core.validation_engine import ValidationEngine
from rules.business_rules import classify_movement, normalize_state
from rules.currencies import detect_currency
from rules.dates import parse_dates_robust
from rules.ids import is_id_column, restore_id_prefix
from rules.prices import parse_price
from rules.products import is_hardware_column, normalize_product_name

logger = setup_logger("transactional_es_cleaner")

_TRANSACTIONAL_TOKENS = (
    "monto", "precio", "total", "amount", "price",
    "fecha", "date", "estado", "status",
    "id_", "_id", "codigo", "trx", "folio", "transaccion",
)


class TransactionalEsCleaner(Cleaner):
    """Fallback cleaner: datos transaccionales con columnas en castellano."""

    name = "transactional_es"

    @staticmethod
    def matches(df: pd.DataFrame) -> bool:
        cols_lower = " ".join(str(c).lower() for c in df.columns)
        return any(tok in cols_lower for tok in _TRANSACTIONAL_TOKENS)

    @staticmethod
    def clean_chunk(df: pd.DataFrame) -> pd.DataFrame:
        df = ValidationEngine.validate_structure(df)
        if df.empty:
            return df

        # data_quality_flags es lista de tags (no string concatenado). Más fácil
        # de iterar, contar, filtrar y serializar a JSON/Parquet sin parsing.
        if "data_quality_flags" not in df.columns:
            df.insert(0, "data_quality_flags", [[] for _ in range(len(df))])

        def _add_flag(mask: pd.Series, tag: str) -> None:
            for idx in df.index[mask]:
                df.at[idx, "data_quality_flags"] = df.at[idx, "data_quality_flags"] + [tag]

        # Normaliza nombres de columna
        df.columns = [re.sub(r"[\s\W]+", "_", str(c).strip().lower()).strip("_")
                      for c in df.columns]

        for col in df.columns:
            if col == "data_quality_flags":
                continue

            if is_id_column(col):
                df[col], stats = restore_id_prefix(df[col])
                if stats["fixed_ids"] > 0:
                    _add_flag(df[col].notna(), "id_fixed")
                continue

            if is_hardware_column(col):
                df[f"{col}_normalized"] = df[col].apply(normalize_product_name)
                continue

            if "fecha" in col or "date" in col:
                df[col], invalid_mask = parse_dates_robust(df[col])
                if invalid_mask.any():
                    _add_flag(invalid_mask, f"invalid_date_{col}")
                continue

            if any(k in col for k in ("monto", "precio", "total", "amount", "price")):
                df[f"{col}_original"] = df[col].copy()
                df[f"{col}_currency"] = df[col].apply(detect_currency)
                df[col] = df[col].apply(parse_price)
                null_mask = df[col].isna()
                if null_mask.any():
                    df.loc[null_mask, col] = 0.0
                    _add_flag(null_mask, f"imputed_0_{col}")
                continue

            if "estado" in col or "status" in col:
                df["estado_normalizado"] = df[col].apply(normalize_state)

        if "tipo_movimiento" not in df.columns:
            df["tipo_movimiento"] = df.apply(classify_movement, axis=1)

        df["quality_score"] = ValidationEngine.compute_quality_score(df)
        return df
