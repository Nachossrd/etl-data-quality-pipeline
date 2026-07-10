"""Cleaner pipeline-friendly que envuelve `core.car_prices_cleaner.CarPricesCleaner`.

El módulo `car_prices_cleaner` opera sobre archivo completo (load -> clean).
Aquí lo adaptamos a la interfaz chunked del pipeline: clean_chunk recibe un
DataFrame que ya viene leído, aplica las 3 reglas del PDF (drop cols, dropna,
binariza transmission) y devuelve el resultado.

Detección: presencia de columnas características de car_prices_*.csv (mmr,
sellingprice + año/year + odometer son señales fuertes).
"""

import pandas as pd

from core.car_prices_cleaner import DROP_COLUMNS, EXPECTED_OUTPUT
from core.cleaners import Cleaner
from core.logging_engine import setup_logger

logger = setup_logger("car_prices_pipeline_cleaner")

# Combinación discriminante: estas 3 juntas son únicas del dataset de autos
_REQUIRED_SIGNALS = ("mmr", "sellingprice", "odometer")


class CarPricesPipelineCleaner(Cleaner):
    name = "car_prices"

    @staticmethod
    def matches(df: pd.DataFrame) -> bool:
        cols = {str(c).lower() for c in df.columns}
        return all(sig in cols for sig in _REQUIRED_SIGNALS)

    @staticmethod
    def clean_chunk(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df

        # Renombra primera columna sin nombre a _c0 (FileExtractor a veces deja "Unnamed: 0")
        if df.columns[0].startswith("Unnamed") or df.columns[0] == "":
            df = df.rename(columns={df.columns[0]: "_c0"})

        present_drops = [c for c in DROP_COLUMNS if c in df.columns]
        if present_drops:
            df = df.drop(columns=present_drops)

        df = df.dropna()

        if "transmission" in df.columns:
            df = df.copy()
            df["transmission"] = (
                df["transmission"].astype(str).str.strip().str.lower()
                .eq("manual").astype(int)
            )

        # Reordena al schema canónico esperado (consistencia con EXPECTED_OUTPUT)
        canonical = ["year", "transmission", "condition", "odometer", "mmr", "sellingprice"]
        ordered = [c for c in canonical if c in df.columns]
        remaining = [c for c in df.columns if c not in ordered]
        df = df[ordered + remaining]

        return df.reset_index(drop=True)
