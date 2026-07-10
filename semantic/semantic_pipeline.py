import pandas as pd
from typing import Tuple, Dict, Any, List, Optional
from semantic.integration_fix import normalize_monetary_column_full
from semantic.temporal_parser import TemporalParser
from semantic.domain_classifier import DomainClassifier
from semantic.movement_classifier import MovementClassifier


class SemanticEnricher:
    """
    Context-Aware Semantic Enricher.

    Before running the heavy phases (FX, domain classification, movement
    classifier), inspects the dataset topology and disables phases that don't
    apply. A Metacritic catalog won't get a `tipo_movimiento` column; an
    accounting CSV won't lose its money parsing.
    """

    MONEY_TOKENS = ('precio', 'monto', 'costo', 'valor', 'total', 'importe', 'amount', 'price')
    PRODUCT_TOKENS = ('producto', 'item', 'descripcion', 'nombre', 'sku', 'articulo', 'product', 'title')
    STATE_TOKENS = ('estado', 'status', 'situacion', 'movimiento')
    NOTE_TOKENS = ('nota', 'obs', 'comentario', 'detalle', 'comment')
    DERIVED_TOKENS = (
        'currency', 'moneda', '_parsed', '_final', '_scale_corrected',
        '_flag', '_domain', '_confidence', '_semantic', '_as_date'
    )

    def __init__(self, enable_money: bool = True, enable_movement: bool = True, enable_domain: bool = True):
        self.enable_money = enable_money
        self.enable_movement = enable_movement
        self.enable_domain = enable_domain

    # ---- Auto-discovery helpers ---------------------------------------------

    @classmethod
    def _is_derived(cls, col: str) -> bool:
        col_lower = str(col).lower()
        return any(tok in col_lower for tok in cls.DERIVED_TOKENS)

    @classmethod
    def _detect_money_columns(cls, df: pd.DataFrame) -> List[str]:
        return [
            c for c in df.columns
            if any(tok in str(c).lower() for tok in cls.MONEY_TOKENS) and not cls._is_derived(c)
        ]

    @classmethod
    def _detect_product_columns(cls, df: pd.DataFrame) -> List[str]:
        return [
            c for c in df.columns
            if any(tok in str(c).lower() for tok in cls.PRODUCT_TOKENS) and not cls._is_derived(c)
        ]

    @classmethod
    def _detect_state_column(cls, df: pd.DataFrame) -> Optional[str]:
        return next(
            (c for c in df.columns
             if any(tok in str(c).lower() for tok in cls.STATE_TOKENS) and not cls._is_derived(c)),
            None,
        )

    @classmethod
    def _detect_note_column(cls, df: pd.DataFrame) -> Optional[str]:
        return next(
            (c for c in df.columns
             if any(tok in str(c).lower() for tok in cls.NOTE_TOKENS) and not cls._is_derived(c)),
            None,
        )

    # ---- Pipeline -----------------------------------------------------------

    def enrich(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """
        Enriches a DataFrame semantically. Applies the Context-Aware Killswitch
        first: disables money/movement/domain phases when the dataset topology
        doesn't justify them.
        """
        df_enriched = df.copy()
        metrics: Dict[str, Any] = {
            "monetary_columns_parsed": [],
            "temporal_columns_parsed": [],
            "domain_columns_classified": [],
            "movement_reclassifications": 0,
            "killswitch": {},
        }

        if df_enriched.empty:
            return df_enriched, metrics

        # --- Context-Aware Killswitch ---
        money_cols = self._detect_money_columns(df)
        product_cols = self._detect_product_columns(df)
        state_col = self._detect_state_column(df)

        is_transactional = bool(money_cols) or bool(state_col)

        if not money_cols and not state_col:
            # No money, no state → not transactional. Skip movement classifier.
            self.enable_movement = False
        if not product_cols or not is_transactional:
            # Domain classification (legit/foreign) only matters when there's
            # money or state flow. A Metacritic catalog with game_title is not
            # a "product expense" — don't tag it.
            self.enable_domain = False
        if not money_cols:
            # Nothing monetary → skip FX/scale normalizer entirely.
            self.enable_money = False

        metrics["killswitch"] = {
            "enable_money": self.enable_money,
            "enable_movement": self.enable_movement,
            "enable_domain": self.enable_domain,
        }

        # --- Phase loop ---
        for col in df.columns:
            col_lower = str(col).lower()
            is_derived = self._is_derived(col)

            # Monetary
            if (self.enable_money
                    and not is_derived
                    and any(k in col_lower for k in self.MONEY_TOKENS)):
                df_enriched = normalize_monetary_column_full(df_enriched, col)
                metrics["monetary_columns_parsed"].append(col)

            # Temporal — fechas/dates puras se procesan SIEMPRE (no son lógica
            # transaccional, sirven para cualquier dataset).
            elif any(k in col_lower for k in ('fecha', 'date', 'creado', 'actualizado')) and not is_derived:
                df_enriched[f"{col}_as_date"] = TemporalParser.parse_column(df[col])
                metrics["temporal_columns_parsed"].append(col)

            # Domain classification (product/SKU)
            elif (self.enable_domain
                    and not is_derived
                    and any(k in col_lower for k in self.PRODUCT_TOKENS)):
                df_enriched[f"{col}_domain_flag"] = DomainClassifier.process_series(df[col])
                metrics["domain_columns_classified"].append(col)

        # --- Movement Classification (gated by killswitch) ---
        if self.enable_movement:
            nota_col = self._detect_note_column(df)
            estado_col = state_col
            if nota_col and estado_col:
                new_col = f"{estado_col}_semantic"
                df_enriched[new_col] = MovementClassifier.process_dataframe(df, nota_col, estado_col)
                reclassifications = (
                    df_enriched[estado_col].astype(str).str.upper()
                    != df_enriched[new_col].astype(str).str.upper()
                ).sum()
                metrics["movement_reclassifications"] = int(reclassifications)

        return df_enriched, metrics

    # ---- Backwards-compatible classmethod ----------------------------------

    @classmethod
    def enrich_dataset(cls, df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """Convenience shortcut: defaults everything to enabled, lets the
        killswitch auto-tune. Equivalent to `SemanticEnricher().enrich(df)`."""
        return cls().enrich(df)
