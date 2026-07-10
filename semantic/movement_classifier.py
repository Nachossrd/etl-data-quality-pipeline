import re
import pandas as pd
from typing import Optional, List


class MovementClassifier:
    SIGNALS = {
        r'prest[eéó]': ('PRESTAMO', 0.95),
        r'devol(vi|ucion)': ('DEVOLUCION', 0.90),
        r'vend(id[oa]|i)': ('VENTA', 0.80),
        r'ajust[eo]': ('AJUSTE', 0.85)
    }

    AMOUNT_TOKENS = ('precio', 'monto', 'costo', 'valor', 'total', 'importe', 'amount', 'price')
    STATE_TOKENS = ('estado', 'status', 'situacion', 'movimiento')
    TEXT_TOKENS = ('nota', 'obs', 'comentario', 'detalle', 'comment', 'descripcion')
    DERIVED_TOKENS = (
        'currency', 'moneda', '_parsed', '_final', '_scale_corrected',
        '_flag', '_domain', '_confidence', '_semantic', '_as_date'
    )

    @classmethod
    def classify(cls, note: str, current_state: str) -> str:
        note_str = str(note).lower() if not pd.isna(note) else ""
        current_state_str = str(current_state).upper() if not pd.isna(current_state) else "UNKNOWN"

        best_signal = None
        highest_weight = 0.0

        for pattern, (state_val, weight) in cls.SIGNALS.items():
            if re.search(pattern, note_str):
                if weight > highest_weight:
                    highest_weight = weight
                    best_signal = state_val

        if best_signal and highest_weight > 0.5:
            return best_signal

        return current_state_str

    # ---- Column discovery helpers (used by classify_dataframe) -------------

    @classmethod
    def _is_derived(cls, col: str) -> bool:
        col_lower = str(col).lower()
        return any(tok in col_lower for tok in cls.DERIVED_TOKENS)

    @classmethod
    def _find_amount_column(cls, df: pd.DataFrame) -> Optional[str]:
        for col in df.columns:
            if cls._is_derived(col):
                continue
            col_lower = str(col).lower()
            if any(tok in col_lower for tok in cls.AMOUNT_TOKENS):
                return col
        return None

    @classmethod
    def _find_state_column(cls, df: pd.DataFrame) -> Optional[str]:
        for col in df.columns:
            if cls._is_derived(col):
                continue
            col_lower = str(col).lower()
            if any(tok in col_lower for tok in cls.STATE_TOKENS):
                return col
        return None

    @classmethod
    def _find_text_columns(cls, df: pd.DataFrame) -> List[str]:
        return [
            c for c in df.columns
            if not cls._is_derived(c)
            and any(tok in str(c).lower() for tok in cls.TEXT_TOKENS)
        ]

    # ---- Dataframe-level classification ------------------------------------

    def classify_dataframe(self, df: pd.DataFrame,
                            amount_col: Optional[str] = None,
                            state_col: Optional[str] = None,
                            text_cols: Optional[List[str]] = None) -> pd.DataFrame:
        """
        Annotates the dataframe with tipo_movimiento / tipo_confianza columns.

        Early exit: if the dataset has neither an amount column NOR a state
        column, it is NOT transactional in nature (e.g. Metacritic catalog,
        survey results). Return the dataframe untouched, no injection.
        """
        if df.empty:
            return df

        if amount_col is None:
            amount_col = self._find_amount_column(df)
        if state_col is None:
            state_col = self._find_state_column(df)
        if text_cols is None:
            text_cols = self._find_text_columns(df)

        # --- Killswitch: nothing transactional to classify ---
        if amount_col is None and state_col is None:
            return df

        df_out = df.copy()
        note_col = text_cols[0] if text_cols else None

        if state_col and note_col:
            df_out['tipo_movimiento'] = df_out.apply(
                lambda row: self.classify(row[note_col], row[state_col]), axis=1
            )
            df_out['tipo_confianza'] = df_out[note_col].apply(self._confidence_for_note)
        elif state_col:
            df_out['tipo_movimiento'] = df_out[state_col].astype(str).str.upper()
            df_out['tipo_confianza'] = 0.5
        else:
            # Only amount present, no state: weak classification, default VENTA
            df_out['tipo_movimiento'] = 'VENTA'
            df_out['tipo_confianza'] = 0.3

        return df_out

    @classmethod
    def _confidence_for_note(cls, note) -> float:
        note_str = str(note).lower() if not pd.isna(note) else ""
        best = 0.0
        for pattern, (_, weight) in cls.SIGNALS.items():
            if re.search(pattern, note_str) and weight > best:
                best = weight
        return best if best > 0 else 0.5

    # ---- Legacy API (kept for existing callers) ----------------------------

    @staticmethod
    def process_dataframe(df: pd.DataFrame, note_col: str, state_col: str) -> pd.Series:
        if note_col not in df.columns or state_col not in df.columns:
            return df[state_col] if state_col in df.columns else pd.Series("UNKNOWN", index=df.index)

        return df.apply(
            lambda row: MovementClassifier.classify(row[note_col], row[state_col]), axis=1
        )
