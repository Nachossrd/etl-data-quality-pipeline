import pandas as pd
import csv
import os
from typing import Iterator
from core.logging_engine import setup_logger
from config.settings import PipelineConfig

logger = setup_logger("file_extraction")

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
    def extract_excel(file_path: str) -> Iterator[pd.DataFrame]:
        """Extracts Excel data. Yields a single chunk containing the best sheet."""
        logger.info(f"Extracting Excel file: {file_path}")
        xl = pd.ExcelFile(file_path)
        best_sheet = xl.sheet_names[0]
        best_score = -1
        best_df = None
        
        for sheet in xl.sheet_names:
            try:
                raw = xl.parse(sheet, header=None, nrows=50)
                header_row = FileExtractor._detect_header_row(raw)
                df = xl.parse(sheet, header=header_row)
            except Exception:
                df = xl.parse(sheet)
                
            score = len(df.columns) + df.notna().sum().sum() / max(len(df), 1)
            if score > best_score:
                best_sheet, best_score, best_df = sheet, score, df
                
        logger.info(f"Selected sheet: {best_sheet} with {len(best_df)} rows")
        # Yield the entire dataframe as one chunk (since Excel doesn't stream well)
        yield best_df

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
