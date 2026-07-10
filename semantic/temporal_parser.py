import pandas as pd
import numpy as np
from typing import Tuple, Optional

class SerialDateDetector:
    # Excel epoch starts at 1899-12-30 (due to 1900 leap year bug)
    EXCEL_EPOCH = pd.Timestamp('1899-12-30')
    MIN_SERIAL = 30000  # Approx 1982
    MAX_SERIAL = 60000  # Approx 2064

    @classmethod
    def is_serial_date_column(cls, series: pd.Series) -> bool:
        if not pd.api.types.is_numeric_dtype(series):
            return False
            
        clean_s = series.dropna()
        if clean_s.empty:
            return False

        # All integers
        if not np.all(np.equal(np.mod(clean_s, 1), 0)):
            return False

        # No negatives and within range
        if (clean_s < 0).any():
            return False
            
        if not (clean_s.between(cls.MIN_SERIAL, cls.MAX_SERIAL)).all():
            return False

        # Coefficient of variation (CV = std/mean) < 0.15
        mean_val = clean_s.mean()
        if mean_val == 0:
            return False
            
        cv = clean_s.std() / mean_val
        if cv >= 0.15:
            return False

        return True

    @classmethod
    def convert_serial(cls, val: float) -> Optional[pd.Timestamp]:
        if pd.isna(val):
            return None
        return cls.EXCEL_EPOCH + pd.to_timedelta(val, unit='D')


class TemporalParser:
    @staticmethod
    def parse_column(series: pd.Series) -> pd.Series:
        if SerialDateDetector.is_serial_date_column(series):
            return series.apply(SerialDateDetector.convert_serial)
            
        # Fallback to general parsing
        parsed = pd.to_datetime(series, errors='coerce', format='mixed', dayfirst=True)
        return parsed
