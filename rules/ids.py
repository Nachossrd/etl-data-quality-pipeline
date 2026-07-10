import re
import pandas as pd
from typing import Tuple, Dict, Any

def restore_id_prefix(series: pd.Series, prefix: str = "TRX-") -> Tuple[pd.Series, Dict[str, Any]]:
    """Normalizes transactional IDs to format TRX-XXXX, handling missing/invalid gracefully."""
    stats = {
        "fixed_ids": 0,
        "null_ids": 0,
        "invalid_ids": 0,
    }

    def _fix(value: Any) -> Any:
        if pd.isna(value):
            stats["null_ids"] += 1
            return pd.NA

        text = str(value).strip()
        numeros = re.findall(r"\d+", text)

        if not numeros:
            stats["invalid_ids"] += 1
            return pd.NA

        stats["fixed_ids"] += 1
        return f"{prefix}{int(numeros[0]):04d}"

    cleaned = series.apply(_fix)
    return cleaned, stats

def is_id_column(col: str) -> bool:
    """Detects if a column is likely an ID column."""
    pattern = re.compile(r"(^id_|_id$|^codigo|trx|folio|transaccion)", re.I)
    return bool(pattern.search(str(col)))
