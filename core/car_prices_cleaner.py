"""
Limpieza específica para car_prices_*.csv del trabajo PySpark MLlib (UAB).

Reglas exactas del PDF (en este orden):
    1. Eliminar columnas: _c0, make, model, trim, body, color, interior,
       vin, state, seller, saledate.
    2. Eliminar filas con al menos un valor ausente.
    3. Binarizar transmission: manual -> 1, resto -> 0.

Salida: DataFrame con columnas [year, transmission, condition, odometer, mmr, sellingprice]
listo para vectorizar y entrenar LinearRegression.
"""

from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

from core.logging_engine import setup_logger

logger = setup_logger("car_prices_cleaner")

DROP_COLUMNS = [
    "_c0", "make", "model", "trim", "body", "color",
    "interior", "vin", "state", "seller", "saledate",
]
EXPECTED_OUTPUT = ["year", "transmission", "condition", "odometer", "mmr", "sellingprice"]


class CarPricesCleaner:
    """Aplica las 3 reglas de limpieza del PDF sobre car_prices_{train,test}.csv."""

    @staticmethod
    def load(path: str | Path) -> pd.DataFrame:
        """Lee el CSV con separador ';' y normaliza el header.

        El CSV trae la primera columna sin nombre (un índice de fila) que el PDF
        llama _c0. Aquí la renombramos para que el drop posterior funcione.
        """
        df = pd.read_csv(
            path,
            sep=";",
            na_values=["", " ", "—", "NA", "N/A"],
            keep_default_na=True,
            low_memory=False,
        )
        if df.columns[0].startswith("Unnamed"):
            df = df.rename(columns={df.columns[0]: "_c0"})
        logger.info(f"Cargado {path}: {len(df):,} filas x {len(df.columns)} columnas")
        return df

    @staticmethod
    def clean(df: pd.DataFrame) -> Tuple[pd.DataFrame, dict]:
        """Aplica las 3 reglas. Devuelve (df_limpio, métricas)."""
        n0 = len(df)

        present = [c for c in DROP_COLUMNS if c in df.columns]
        missing = [c for c in DROP_COLUMNS if c not in df.columns]
        if missing:
            logger.warning(f"Columnas a eliminar no encontradas: {missing}")
        df = df.drop(columns=present)
        logger.info(f"Drop columnas: {present}")

        n_before_na = len(df)
        df = df.dropna()
        n_after_na = len(df)
        logger.info(f"Drop NA: {n_before_na - n_after_na:,} filas eliminadas")

        if "transmission" not in df.columns:
            raise KeyError("Falta columna 'transmission' tras el drop")
        df = df.copy()
        df["transmission"] = (
            df["transmission"].astype(str).str.strip().str.lower().eq("manual").astype(int)
        )
        n_manual = int(df["transmission"].sum())
        logger.info(f"Binarizado transmission: {n_manual:,} manual (1), "
                    f"{len(df) - n_manual:,} no-manual (0)")

        extras = [c for c in df.columns if c not in EXPECTED_OUTPUT]
        if extras:
            logger.warning(f"Columnas extra inesperadas: {extras}")
        faltantes = [c for c in EXPECTED_OUTPUT if c not in df.columns]
        if faltantes:
            raise ValueError(f"Columnas esperadas ausentes en salida: {faltantes}")

        metrics = {
            "rows_in": n0,
            "rows_out": len(df),
            "rows_dropped_na": n_before_na - n_after_na,
            "manual_count": n_manual,
            "columns_out": list(df.columns),
        }
        return df[EXPECTED_OUTPUT], metrics

    @classmethod
    def process_file(cls, input_path: str | Path,
                     output_path: Optional[str | Path] = None) -> Tuple[pd.DataFrame, dict]:
        """Pipeline completo: load -> clean -> (opcional) export."""
        df = cls.load(input_path)
        clean_df, metrics = cls.clean(df)
        if output_path is not None:
            clean_df.to_csv(output_path, index=False)
            logger.info(f"Exportado limpio a {output_path}: {len(clean_df):,} filas")
        return clean_df, metrics


if __name__ == "__main__":
    import argparse, json

    parser = argparse.ArgumentParser(description="Limpia car_prices según PDF UAB")
    parser.add_argument("--input", required=True, help="Ruta al CSV crudo")
    parser.add_argument("--output", required=True, help="Ruta del CSV limpio de salida")
    args = parser.parse_args()

    _, metrics = CarPricesCleaner.process_file(args.input, args.output)
    print(json.dumps(metrics, indent=2, ensure_ascii=False))
