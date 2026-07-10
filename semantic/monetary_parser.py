import re
import pandas as pd
from typing import Optional, Tuple

class MonetaryParser:
    MULTIPLIERS = {
        'k': 1_000,
        'mil': 1_000,
        'miles': 1_000,
        'lucas': 1_000,
        'palo': 1_000_000,
        'palos': 1_000_000,
        'm': 1_000_000,
        'millon': 1_000_000,
        'millones': 1_000_000
    }

    TEXT_TO_NUM = {
        'uno': 1, 'dos': 2, 'tres': 3, 'cuatro': 4, 'cinco': 5,
        'seis': 6, 'siete': 7, 'ocho': 8, 'nueve': 9, 'diez': 10,
        'veinte': 20, 'treinta': 30, 'cuarenta': 40, 'cincuenta': 50,
        'sesenta': 60, 'setenta': 70, 'ochenta': 80, 'noventa': 90,
        'cien': 100, 'un': 1
    }

    @classmethod
    def parse_value(cls, val: str) -> Optional[float]:
        if pd.isna(val) or val is None:
            return None
            
        val_str = str(val).lower().strip()
        val_str = re.sub(r'[^a-z0-9\s.,]', '', val_str).strip()

        # Extract textual numbers if present
        multiplier = 1.0
        
        # Check multipliers
        for word, mult in cls.MULTIPLIERS.items():
            # Check if word is whole or attached to number (e.g. 350k)
            pattern = r'(?:\b|\d)' + word + r'\b'
            if re.search(pattern, val_str):
                multiplier *= mult
                val_str = re.sub(word + r'\b', '', val_str).strip()

        for word in val_str.split():
            if word in cls.TEXT_TO_NUM:
                # E.g. "sesenta" -> 60
                val_str = re.sub(r'\b' + word + r'\b', str(cls.TEXT_TO_NUM[word]), val_str).strip()

        # Remove any remaining alphabetical characters (like currency codes)
        val_str = re.sub(r'[a-z]', '', val_str)

        # Handle 1.200.000 vs 1,200.00 vs 1.2
        # DFA logic approximation
        val_str = val_str.replace(' ', '')
        if not val_str:
            return None

        # Check for multiple dots or dots before 3 digits (thousands separator)
        if re.match(r'^\d{1,3}(\.\d{3})+$', val_str):
            val_str = val_str.replace('.', '')
        # Check for multiple commas or commas before 3 digits
        elif re.match(r'^\d{1,3}(,\d{3})+$', val_str):
            val_str = val_str.replace(',', '')
        elif ',' in val_str and '.' in val_str:
            if val_str.rfind(',') > val_str.rfind('.'):
                val_str = val_str.replace('.', '').replace(',', '.')
            else:
                val_str = val_str.replace(',', '')
        else:
            # Single separator
            if ',' in val_str:
                val_str = val_str.replace(',', '.')

        try:
            numeric_val = float(val_str)
            return numeric_val * multiplier
        except ValueError:
            return None

class MonetaryParserBatch:
    @staticmethod
    def process_series(series: pd.Series) -> pd.Series:
        return series.apply(MonetaryParser.parse_value)
