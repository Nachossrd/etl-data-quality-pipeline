import pandas as pd
import csv
import os
import re
import unicodedata
from typing import Iterator, List, Set
from core.logging_engine import setup_logger
from config.settings import PipelineConfig

logger = setup_logger("file_extraction")

# Fracción de columnas de la hoja principal que otra hoja debe compartir para
# considerarse "la misma tabla partida en varias hojas" (típico: un workbook
# con "Ventas 2019-2020" y "Ventas 2021-2022").
SHEET_SCHEMA_OVERLAP = 0.70


def _normalize_col(col: object) -> str:
    """Nombre de columna comparable: sin acentos, sin puntuación, minúsculas."""
    text = unicodedata.normalize("NFKD", str(col))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text.lower())


class FileExtractor:
    @staticmethod
    def _detect_header_row(raw: pd.DataFrame, max_scan: int = 10) -> int:
        best_row = 0
        best_score = -999
        n_rows = min(max_scan, len(raw))

        for i in range(n_rows):
            row = raw.iloc[i]
            non_null = row.dropna()
            if non_null.empty:
                continue

            score = 0
            str_count = sum(1 for v in non_null if isinstance(v, str) and str(v).strip())
            score += 2 if str_count / max(len(non_null), 1) > 0.6 else 0

            if i + 1 < len(raw):
                next_row = raw.iloc[i + 1]
                numeric_next = pd.to_numeric(next_row, errors="coerce").notna().sum()
                score += 2 if numeric_next > 0 else 0

            null_ratio = row.isna().mean()
            score += 1 if null_ratio < 0.3 else 0

            unnamed_count = sum(1 for v in non_null if isinstance(v, str) and str(v).lower().startswith("unnamed"))
            score -= unnamed_count

            if score > best_score:
                best_score = score
                best_row = i

        return best_row

    @staticmethod
    def _sheet_columns(df: pd.DataFrame) -> Set[str]:
        return {_normalize_col(c) for c in df.columns}

    @staticmethod
    def extract_excel(file_path: str) -> Iterator[pd.DataFrame]:
        """Extrae un Excel. Emite la mejor hoja + toda hoja con el mismo esquema.

        Antes esta función elegía "la mejor hoja" y descartaba el resto en
        silencio. Para un workbook partido por período (`Ventas 2019 - 2020` /
        `Ventas 2021 - 2022`) eso significaba perder la mitad del dataset sin
        que ningún log lo dijera: el pipeline reportaba éxito sobre datos
        incompletos, que es peor que fallar.

        Ahora se puntúa cada hoja igual que antes y, además de la ganadora, se
        emiten como chunks adicionales las hojas que compartan al menos
        SHEET_SCHEMA_OVERLAP de sus columnas (comparadas sin acentos ni
        puntuación, porque los headers cambian de un año a otro). Las hojas con
        esquema distinto — un "Diccionario" o un "Resumen" — se siguen
        descartando, pero ahora queda registrado en el log.

        Cuando se une más de una hoja se agrega la columna `_hoja_origen` para
        que el dato nunca pierda su trazabilidad. Con una sola hoja el
        comportamiento es idéntico al anterior (sin columnas extra).
        """
        logger.info(f"Extracting Excel file: {file_path}")
        xl = pd.ExcelFile(file_path)

        parsed: dict[str, pd.DataFrame] = {}
        best_sheet = xl.sheet_names[0]
        best_score = -1.0

        for sheet in xl.sheet_names:
            try:
                raw = xl.parse(sheet, header=None, nrows=50)
                header_row = FileExtractor._detect_header_row(raw)
                df = xl.parse(sheet, header=header_row)
            except Exception:
                df = xl.parse(sheet)

            parsed[sheet] = df
            score = len(df.columns) + df.notna().sum().sum() / max(len(df), 1)
            if score > best_score:
                best_sheet, best_score = sheet, score

        best_cols = FileExtractor._sheet_columns(parsed[best_sheet])
        compatible: List[str] = []
        for sheet in xl.sheet_names:
            if sheet == best_sheet:
                compatible.append(sheet)
                continue
            df = parsed[sheet]
            if df.empty or not best_cols:
                continue
            overlap = len(FileExtractor._sheet_columns(df) & best_cols) / len(best_cols)
            if overlap >= SHEET_SCHEMA_OVERLAP:
                compatible.append(sheet)
            else:
                logger.warning(
                    f"Hoja '{sheet}' descartada: sólo comparte "
                    f"{overlap*100:.0f}% del esquema de '{best_sheet}' "
                    f"(mínimo {SHEET_SCHEMA_OVERLAP*100:.0f}%)"
                )

        # Orden original del workbook, con la hoja ganadora primero para que el
        # header del CSV de salida salga de la hoja de esquema más completo.
        compatible.sort(key=lambda s: (s != best_sheet, xl.sheet_names.index(s)))
        total_rows = sum(len(parsed[s]) for s in compatible)

        if len(compatible) == 1:
            logger.info(f"Selected sheet: {best_sheet} with {total_rows} rows")
            yield parsed[best_sheet]
            return

        logger.info(
            f"Unificando {len(compatible)} hojas con esquema compatible "
            f"({', '.join(compatible)}) — {total_rows} filas en total"
        )
        for sheet in compatible:
            df = parsed[sheet].copy()
            df["_hoja_origen"] = sheet
            yield df

    @staticmethod
    def extract_csv_chunked(file_path: str) -> Iterator[pd.DataFrame]:
        """Extracts CSV data in chunks with automatic delimiter detection."""
        logger.info(f"Extracting CSV file: {file_path}")
        encodings = ["utf-8", "utf-8-sig", "latin-1", "iso-8859-1"]
        
        for enc in encodings:
            try:
                # errors="strict" para que utf-8 inválido falle y caigamos al
                # próximo encoding. El bug anterior usaba "replace" lo que
                # silenciosamente reemplazaba bytes inválidos con '?', haciendo
                # que utf-8 "siempre funcionara" y la lista de fallbacks fuera
                # decorativa (latin-1/iso nunca se probaban).
                with open(file_path, "r", encoding=enc, errors="strict") as f:
                    sample = f.read(8192)
                    try:
                        dialect = csv.Sniffer().sniff(sample, delimiters=[",", ";", "\t", "|"])
                        sep = dialect.delimiter
                    except Exception:
                        sep = ","

                # Detect header using a small sample
                raw = pd.read_csv(file_path, sep=sep, engine="python", encoding=enc, header=None, on_bad_lines="warn", nrows=50)
                header_row = FileExtractor._detect_header_row(raw)
                logger.info(f"Header detected at row: {header_row} (Encoding: {enc}, Sep: '{sep}')")

                # Stream the file using C engine where possible
                for chunk in pd.read_csv(
                    file_path, 
                    sep=sep, 
                    encoding=enc, 
                    header=header_row, 
                    on_bad_lines="warn",
                    chunksize=PipelineConfig.CHUNK_SIZE
                ):
                    yield chunk
                return  # If successful, exit the encoding loop
            except Exception as exc:
                logger.debug(f"Encoding {enc} failed: {exc}")
                
        raise ValueError("Could not parse CSV with any known encoding.")
