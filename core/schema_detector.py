import pandas as pd
from typing import Dict, Any

class SchemaDetector:
    @staticmethod
    def detect_schema(df: pd.DataFrame) -> Dict[str, Any]:
        """Infers the database schema (types, nullability, unique values) for a given dataframe."""
        schema = {}
        for col in df.columns:
            schema[col] = {
                "dtype": str(df[col].dtype),
                "null_ratio": df[col].isna().mean(),
                "unique_ratio": df[col].nunique() / max(len(df), 1) if not df.empty else 0,
                "is_candidate_key": df[col].is_unique if not df.empty else False
            }
        return schema
