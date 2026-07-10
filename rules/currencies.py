import re
import pandas as pd
from typing import Any, Optional

CURRENCY_SYMBOLS = {
    "$": "CLP",
    "US$": "USD",
    "USD": "USD",
    "€": "EUR",
    "EUR": "EUR",
    "CLP": "CLP",
}

CURRENCY_KEYWORDS = {
    "usd": "USD",
    "dolar": "USD",
    "dólar": "USD",
    "dolares": "USD",
    "euros": "EUR",
    "euro": "EUR",
    "clp": "CLP",
    "peso": "CLP",
    "pesos": "CLP",
    "lucas": "CLP",
}

def detect_currency(text: Any) -> str:
    if pd.isna(text):
        return "DESCONOCIDA"
        
    raw = str(text).strip().lower()
    
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol.lower() in raw:
            return code
            
    for keyword, code in CURRENCY_KEYWORDS.items():
        if re.search(rf"\b{re.escape(keyword)}\b", raw):
            return code
            
    return "DESCONOCIDA"
