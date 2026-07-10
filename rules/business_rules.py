import re
import pandas as pd
from typing import Any

STATE_CATALOG = {
    "vendido": "VENDIDO",
    "comprado": "COMPRADO",
    "pendiente": "PENDIENTE",
    "devuelto": "DEVUELTO",
    "cambio": "CAMBIO",
}

def normalize_state(value: Any) -> str:
    if pd.isna(value):
        return "DESCONOCIDO"
    text = str(value).strip().lower()
    return STATE_CATALOG.get(text, "DESCONOCIDO")

def classify_movement(row: pd.Series) -> str:
    amount = None
    state = str(row.get("estado", "")).strip().lower()
    
    # Try to find monto or price
    for col in row.index:
        if "monto" in col.lower() or "precio" in col.lower():
            try:
                amount = float(row[col])
                break
            except (ValueError, TypeError):
                continue

    if amount is not None and amount < 0:
        if "devol" in state:
            return "DEVOLUCION"
        if "cambio" in state or "ajuste" in state:
            return "AJUSTE"
        return "REEMBOLSO"

    if "devol" in state:
        return "DEVOLUCION"
    if "cambio" in state:
        return "CAMBIO"
    if "ajuste" in state:
        return "AJUSTE"
    if "reembol" in state:
        return "REEMBOLSO"
    return "VENTA"

def is_critical_field(col: str) -> bool:
    pattern = re.compile(r"(fecha|monto|venta|precio|total|id)", re.I)
    return bool(pattern.search(str(col)))
