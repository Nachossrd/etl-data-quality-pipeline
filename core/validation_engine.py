import pandas as pd
from rules.business_rules import is_critical_field

class ValidationEngine:
    @staticmethod
    def validate_structure(df: pd.DataFrame) -> pd.DataFrame:
        """Fixes structural issues like duplicate columns or broken whitespace."""
        df = df.copy()
        # Drop completely empty rows or columns
        df.dropna(how='all', inplace=True)
        df.dropna(axis=1, how='all', inplace=True)
        
        # Deduplicate column names
        cols = pd.Series(df.columns)
        for dup in cols[cols.duplicated()].unique():
            cols[cols[cols == dup].index.values.tolist()] = [dup + '_' + str(i) if i != 0 else dup for i in range(sum(cols == dup))]
        df.columns = cols
        
        return df

    @staticmethod
    def compute_quality_score(df: pd.DataFrame) -> pd.Series:
        """Computes a quality score (0-100) for each row."""
        score = pd.Series(100, index=df.index)
        critical_cols = [c for c in df.columns if is_critical_field(c)]
        
        for col in critical_cols:
            score -= df[col].isna().astype(int) * 20
            
        if "estado_normalizado" in df.columns:
            score -= (df["estado_normalizado"] == "DESCONOCIDO").astype(int) * 10
            
        if "data_quality_flags" in df.columns:
            flag_penalty = df["data_quality_flags"].apply(
                lambda flags: any(
                    "missing_" in t or "unknown" in t
                    for t in (flags if isinstance(flags, list) else [])
                )
            ).astype(int) * 10
            score -= flag_penalty
            
        return score.clip(lower=0, upper=100)
