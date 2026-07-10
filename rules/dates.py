import pandas as pd
from typing import Tuple

def parse_dates_robust(series: pd.Series) -> Tuple[pd.Series, pd.Series]:
    """
    Parses dates robustly supporting mixed formats and dayfirst.
    Returns:
        Tuple of (parsed_dates, invalid_dates_mask)
    """
    candidate = series.astype(str).str.strip()
    candidate = candidate.replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
    
    # ISO formats like YYYY-MM-DD
    year_first = candidate.str.match(r"^\d{4}[-/]\d{2}[-/]\d{2}$", na=False)
    
    parsed_mixed = pd.to_datetime(
        candidate.where(~year_first),
        errors="coerce",
        format="mixed",
        dayfirst=True
    )
    
    parsed_iso = candidate.where(year_first).apply(
        lambda x: pd.to_datetime(x, errors="coerce", dayfirst=False)
    )
    
    date_series = parsed_mixed.combine_first(parsed_iso)
    
    # Check for dates that were valid strings but couldn't be parsed
    invalid_mask = candidate.notna() & date_series.isna()
    
    return date_series, invalid_mask
