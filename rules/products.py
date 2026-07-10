import re
import pandas as pd
from typing import Any, Optional

def normalize_product_name(value: Any) -> Optional[str]:
    if pd.isna(value):
        return None
    text = str(value).strip().upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None

def is_hardware_column(col: str) -> bool:
    pattern = re.compile(r"(hardware|item|modelo|sku|producto)", re.I)
    return bool(pattern.search(str(col)))
