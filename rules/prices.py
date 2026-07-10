"""Parser robusto de montos monetarios con separadores heterogéneos.

Maneja correctamente:
    "$1.200.000"          -> 1_200_000   (es-locale, dot thousand sep)
    "$1,200,000"          -> 1_200_000   (en-locale, comma thousand sep)
    "1.200,50"            -> 1200.50     (es-locale, dot=thousand, comma=decimal)
    "1,200.50"            -> 1200.50     (en-locale, comma=thousand, dot=decimal)
    "15.50"               -> 15.50       (en decimal)
    "15,50"               -> 15.50       (es decimal)
    "40 mil"              -> 40_000      (palabra de magnitud)
    "sesenta lucas"       -> 60_000      (números en palabras + magnitud)
    "un millon"           -> 1_000_000   (idem)
    "-500" / "+1.5"       -> con signo

Regla del separador heterogéneo:
    Si hay AMBOS '.' y ',': el más a la derecha es el decimal.
    Si solo hay un separador repetido (>1): todos son miles.
    Si solo hay uno y termina en 3 dígitos: ambiguo -> tratado como miles
    Si solo hay uno y termina en 1-2 dígitos: decimal.
"""

import re
import pandas as pd
from typing import Any, Optional

NUMBER_WORDS = {
    "cero": 0, "un": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11,
    "doce": 12, "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16,
    "dieciséis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19,
    "veinte": 20, "veintiuno": 21, "veintidos": 22, "veintidós": 22,
    "veintitres": 23, "veintitrés": 23, "veinticuatro": 24, "veinticinco": 25,
    "veintiseis": 26, "veintiséis": 26, "veintisiete": 27, "veintiocho": 28,
    "veintinueve": 29, "treinta": 30, "cuarenta": 40, "cincuenta": 50,
    "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90,
    "cien": 100, "ciento": 100, "doscientos": 200, "trescientos": 300,
    "cuatrocientos": 400, "quinientos": 500, "seiscientos": 600,
    "setecientos": 700, "ochocientos": 800, "novecientos": 900,
}

MAGNITUDE_WORDS = {
    "k": 1_000,
    "m": 1_000_000,
    "mil": 1_000,
    "miles": 1_000,
    "millon": 1_000_000,
    "millones": 1_000_000,
    "lucas": 1_000,
    "palo": 1_000_000,
    "palos": 1_000_000,
}


def text_to_number(text: str) -> Optional[float]:
    """Convierte texto en castellano a número. Aplica magnitudes internamente."""
    tokens = re.sub(r"[^a-záéíóúüñ0-9\s]", " ", text.lower()).split()
    total = 0
    current = 0
    for token in tokens:
        if token == "y":
            continue
        if token in NUMBER_WORDS:
            current += NUMBER_WORDS[token]
            continue
        if token in MAGNITUDE_WORDS:
            multiplier = MAGNITUDE_WORDS[token]
            if current == 0:
                current = 1
            total += current * multiplier
            current = 0
            continue
        if token.isdigit():
            current += int(token)
            continue
    result = total + current
    return float(result) if result > 0 else None


def _parse_numeric_string(num_str: str) -> Optional[float]:
    """Decodifica separadores. Recibe ya solo dígitos, '.', ',', signo opcional."""
    sign = ""
    if num_str and num_str[0] in "+-":
        sign = num_str[0]
        num_str = num_str[1:]
    if not num_str:
        return None

    has_dot = "." in num_str
    has_comma = "," in num_str

    if has_dot and has_comma:
        # El más a la derecha es decimal
        if num_str.rfind(",") > num_str.rfind("."):
            num_str = num_str.replace(".", "").replace(",", ".")
        else:
            num_str = num_str.replace(",", "")
    elif has_dot:
        parts = num_str.split(".")
        if len(parts) > 2:
            # Múltiples '.' -> todos son miles. "1.200.000" -> "1200000"
            num_str = num_str.replace(".", "")
        else:
            last = parts[-1]
            if len(last) == 3 and len(parts[0]) >= 1 and len(parts[0]) <= 3:
                # "1.500" -> ambiguo. Heurística: trata como miles (más común en negocios).
                # Casos como "1.5" (decimal) tienen último grupo de 1 dígito -> caen en else.
                num_str = num_str.replace(".", "")
            # else: decimal estándar ("15.50", "1.5")
    elif has_comma:
        parts = num_str.split(",")
        if len(parts) > 2:
            num_str = num_str.replace(",", "")
        else:
            last = parts[-1]
            if len(last) == 3 and len(parts[0]) >= 1 and len(parts[0]) <= 3:
                num_str = num_str.replace(",", "")
            else:
                num_str = num_str.replace(",", ".")

    try:
        return float(sign + num_str)
    except ValueError:
        return None


def parse_price(text: Any) -> Optional[float]:
    if pd.isna(text):
        return None

    raw = str(text).strip()
    if not raw:
        return None

    # Normaliza espacios Unicode
    normalized = raw.replace(" ", " ").replace(" ", " ")
    # Deja solo dígitos, signo, separadores y espacios para el regex
    cleaned = re.sub(r"[^\d.,+\-\s]", " ", normalized)
    # Si hay espacios entre dígitos, son separadores de miles europeo
    # (común en Excel y reportes financieros): "1 500" -> "1500"
    cleaned = re.sub(r"(?<=\d)\s+(?=\d)", "", cleaned)

    # Captura digit-groups separados por '.' o ',' contiguos
    match = re.search(r"([+-]?\d+(?:[.,]\d+)*)", cleaned)
    amount: Optional[float] = None

    if match:
        amount = _parse_numeric_string(match.group(1))
        if amount is not None:
            # Aplica multiplicador de magnitud SOLO al amount numérico,
            # no al de text_to_number (que ya lo aplica internamente).
            text_lower = str(text).lower()
            for word, mult in MAGNITUDE_WORDS.items():
                if re.search(rf"\b{re.escape(word)}\b", text_lower):
                    amount *= mult
                    break

    if amount is None:
        # Fallback puramente textual
        amount = text_to_number(raw)

    return amount
