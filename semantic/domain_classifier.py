import re
import pandas as pd
from typing import Dict

class DomainClassifier:
    BRANDS = {'logitech', 'samsung', 'apple', 'dell', 'hp', 'lenovo', 'asus', 'sony'}
    EXCLUSIONS = {'pizza', 'flete', 'empanada', 'empanadas', 'transporte', 'envio', 'comida'}
    
    TECH_PATTERNS = [
        r'\d{1,3}gb',      # 16GB, 8gb
        r'\d{1,3}tb',      # 1tb
        r'rtx\s?\d{4}',    # RTX 4090
        r'gtx\s?\d{4}',    # GTX 1080
        r'rx\s?\d{4}',     # RX 6800
        r'core\s?i\d',     # Core i7
        r'ryzen\s?\d',     # Ryzen 5
    ]

    @classmethod
    def score_text(cls, text: str) -> float:
        if pd.isna(text) or not isinstance(text, str):
            return 0.0
            
        text_lower = text.lower()
        score = 0.0

        for brand in cls.BRANDS:
            if brand in text_lower:
                score += 0.5
                break

        for pattern in cls.TECH_PATTERNS:
            if re.search(pattern, text_lower):
                score += 0.3

        for excl in cls.EXCLUSIONS:
            if excl in text_lower:
                score -= 0.7

        return score

    @classmethod
    def classify(cls, text: str) -> str:
        score = cls.score_text(text)
        if score < 0:
            return 'foreign'
        elif score > 0:
            return 'tech_product'
        else:
            return 'unknown'

    @staticmethod
    def process_series(series: pd.Series) -> pd.Series:
        return series.apply(DomainClassifier.classify)
