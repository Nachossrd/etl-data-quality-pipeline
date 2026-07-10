"""ExportEngine: persistencia de chunks limpios a CSV/Parquet/Excel.

Características:
    - CSV append mode para chunking (no carga todo en memoria al final)
    - Parquet snappy via pyarrow
    - Excel auto-split: si el dataset excede EXCEL_ROWS_PER_SHEET, se reparte
      en múltiples hojas (Datos_001, Datos_002, ...) en lugar de skip silencioso
    - Serialización legible de `data_quality_flags` (list -> "tag1;tag2") para
      CSV/Excel; Parquet mantiene la list nativa
"""

import gc
import os
from typing import List

import pandas as pd

from config.settings import PipelineConfig
from core.logging_engine import setup_logger

logger = setup_logger("export_engine")


def _serialize_flags_for_text(df: pd.DataFrame) -> pd.DataFrame:
    """Convierte data_quality_flags (list) a string 'tag1;tag2' para CSV/Excel.

    Pandas serializaría una list como "[id_fixed, missing_date]" (con corchetes
    y comillas) — feo de leer. Esto produce strings limpios.
    Devuelve copia; no muta el df original.
    """
    if "data_quality_flags" not in df.columns:
        return df
    df = df.copy()
    df["data_quality_flags"] = df["data_quality_flags"].apply(
        lambda v: ";".join(v) if isinstance(v, list) else ("" if v is None else str(v))
    )
    return df


class ExportEngine:
    @staticmethod
    def export_chunk(df: pd.DataFrame, table_name: str,
                     mode: str = "a", header: bool = False) -> None:
        if df.empty:
            return

        os.makedirs(PipelineConfig.OUTPUT_DIR, exist_ok=True)
        csv_path = os.path.join(PipelineConfig.OUTPUT_DIR, f"{table_name}_clean.csv")

        # Serializa flags a string antes del CSV
        df_out = _serialize_flags_for_text(df)
        df_out.to_csv(csv_path, mode=mode, header=header, index=False)
        gc.collect()

    @staticmethod
    def convert_csv_to_parquet(table_name: str) -> None:
        """Convierte el CSV final a Parquet (lists nativas) + Excel (string)."""
        csv_path = os.path.join(PipelineConfig.OUTPUT_DIR, f"{table_name}_clean.csv")
        parquet_path = os.path.join(PipelineConfig.OUTPUT_DIR, f"{table_name}_clean.parquet")

        if not os.path.exists(csv_path):
            return

        logger.info(f"Converting {csv_path} to Parquet and Excel formats...")
        try:
            df = pd.read_csv(csv_path)
            df.to_parquet(parquet_path, engine="pyarrow", compression="snappy")
            ExportEngine.export_excel(df, table_name)
            gc.collect()
            logger.info(f"Conversion complete for {table_name}.")
        except Exception as e:
            logger.error(f"Error converting {table_name}: {e}")

    # ─── Excel: auto-split en lugar de skip silencioso ────────────────────
    @staticmethod
    def export_excel(df: pd.DataFrame, table_name: str) -> List[str]:
        """Exporta a Excel. Si excede el límite por hoja, se reparte en hojas
        múltiples dentro del MISMO workbook. Devuelve los nombres de hoja escritos.

        Comportamiento previo: si la tabla tenía >1M filas, el método solo
        logueaba 'too large' y no producía archivo. Eso era un fallo silencioso:
        el usuario veía el pipeline 'exitoso' y luego no encontraba el XLSX.
        """
        excel_path = os.path.join(PipelineConfig.OUTPUT_DIR, f"{table_name}_clean.xlsx")
        rows_per_sheet = PipelineConfig.EXCEL_ROWS_PER_SHEET
        n_rows = len(df)

        # Serializar flags antes de Excel
        df = _serialize_flags_for_text(df)

        sheet_names: List[str] = []
        if n_rows <= rows_per_sheet:
            df.to_excel(excel_path, index=False, sheet_name="Datos")
            sheet_names.append("Datos")
        else:
            # Split en chunks de rows_per_sheet. openpyxl soporta hasta 1.048.576 filas
            # por hoja, así que 1M es seguro.
            n_sheets = (n_rows + rows_per_sheet - 1) // rows_per_sheet
            logger.warning(
                f"Tabla {table_name} tiene {n_rows:,} filas — supera "
                f"{rows_per_sheet:,}/hoja. Generando {n_sheets} hojas."
            )
            with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
                for i in range(n_sheets):
                    start = i * rows_per_sheet
                    end = min(start + rows_per_sheet, n_rows)
                    sheet_name = f"Datos_{i+1:03d}"
                    df.iloc[start:end].to_excel(writer, sheet_name=sheet_name, index=False)
                    sheet_names.append(sheet_name)
        logger.info(f"Excel exportado: {excel_path} ({len(sheet_names)} hoja(s))")
        return sheet_names
