import numpy as np
import pandas as pd
from dataclasses import dataclass
from enum import Enum
from typing import List, Tuple, Dict, Optional

class CorrectionConfidence(Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"

@dataclass
class ScaleCorrection:
    original_value: float
    corrected_value: float
    confidence: CorrectionConfidence
    reason: str

@dataclass
class ColumnScaleReport:
    column_name: str
    total_processed: int
    total_corrected: int
    median_anchor: float
    inferred_currency: str

class CurrencyInferenceEngine:
    @staticmethod
    def infer_currency(currency_series: pd.Series, numeric_series: pd.Series, default_currency: str = "CLP") -> pd.Series:
        """
        Infers currency combining explicit counts with statistical sanity checks.
        Prevents the 'Bayesian Trap' by ensuring the unlabeled magnitude matches the inferred currency.
        """
        clean_cur = currency_series.dropna().astype(str).str.strip().str.upper()
        clean_cur = clean_cur[clean_cur != '']
        
        inferred_series = currency_series.copy()
        
        if clean_cur.empty:
            return inferred_series.fillna(default_currency)

        counts = clean_cur.value_counts()
        total_explicit = counts.sum()
        top_currency = counts.index[0]
        top_count = counts.iloc[0]

        # Get rows without explicit currency
        unlabeled_mask = currency_series.isna() | (currency_series.astype(str).str.strip() == '')
        unlabeled_amounts = numeric_series[unlabeled_mask].dropna()

        # Decide if we can propagate the top currency
        should_propagate = False
        
        if (top_count / total_explicit) > 0.80:
            if top_currency in ["USD", "EUR"] and not unlabeled_amounts.empty:
                # Sanity check: If median is huge, it's NOT USD. It's likely CLP.
                unlabeled_median = unlabeled_amounts.median()
                if unlabeled_median > 5000:
                    should_propagate = False
                else:
                    should_propagate = True
            else:
                should_propagate = True

        if should_propagate:
            inferred_series[unlabeled_mask] = top_currency
        else:
            inferred_series[unlabeled_mask] = default_currency

        return inferred_series

class ColumnScaleNormalizer:
    OUTLIER_RATIO_LOW = 20.0
    OUTLIER_RATIO_HIGH = 20.0
    CORRECTION_RANGE = 100.0

    @staticmethod
    def _calculate_log_z_score(val: float, median: float) -> float:
        if val <= 0 or median <= 0:
            return float('inf')
        return abs(np.log(val) - np.log(median))

    @classmethod
    def normalize_series(
        cls, 
        series: pd.Series, 
        domain_series: pd.Series = None, 
        currency_series: pd.Series = None,
        domain_range_min: float = 5000.0,
        domain_range_max: float = 5000000.0,
        disable_scale_down: bool = False
    ) -> Tuple[pd.Series, pd.Series, ColumnScaleReport]:
        """
        Normalizes a pandas series using strict domain bounds to prevent median poisoning.
        """
        s_numeric = pd.to_numeric(series, errors='coerce')
        corrected_series = s_numeric.copy()
        correction_flags = pd.Series(False, index=series.index)
        
        if domain_series is None:
            domain_series = pd.Series('tech_product', index=series.index)
            
        if currency_series is None:
            currency_series = pd.Series('CLP', index=series.index)

        # Exclude 'foreign' domains from median calculation and normalization
        valid_domain_mask = ~domain_series.isin(['foreign', 'unlikely'])
        valid_data = s_numeric[valid_domain_mask].dropna()
        
        if len(valid_data) < 1:
            return corrected_series, correction_flags, ColumnScaleReport(series.name or "unknown", len(valid_data), 0, 0.0, "UNKNOWN")

        # Step 1: Identify Anchors. Values outside domain bounds are strictly excluded.
        anchors_in_range = valid_data[(valid_data >= domain_range_min) & (valid_data <= domain_range_max)]
        
        if len(anchors_in_range) >= 1:
            q1 = anchors_in_range.quantile(0.25)
            q3 = anchors_in_range.quantile(0.75)
            iqr = q3 - q1
            
            if iqr == 0:
                iqr = anchors_in_range.std() if anchors_in_range.std() > 0 else 1.0

            lower_bound = max(domain_range_min, q1 - 1.5 * iqr)
            upper_bound = min(domain_range_max, q3 + 1.5 * iqr)
            anchor_values = anchors_in_range[(anchors_in_range >= lower_bound) & (anchors_in_range <= upper_bound)]
            anchor_median = anchor_values.median() if not anchor_values.empty else anchors_in_range.median()
        else:
            anchor_median = valid_data.median()

        if pd.isna(anchor_median) or anchor_median == 0:
            return corrected_series, correction_flags, ColumnScaleReport(series.name or "unknown", len(valid_data), 0, 0.0, "UNKNOWN")

        corrections_count = 0

        # Process each row
        for idx, val in s_numeric.items():
            if pd.isna(val) or val <= 0:
                continue
                
            if domain_series.loc[idx] in ['foreign', 'unlikely']:
                continue

            row_currency = currency_series.loc[idx] if idx in currency_series.index else "CLP"
            scale_factor = 1000.0 if row_currency not in ["USD", "EUR"] else 1.0
            can_scale_down = not disable_scale_down

            # Evaluate Z-Scores in log space (Log-Normal distribution of prices)
            z_original = cls._calculate_log_z_score(val, anchor_median)

            if val < (anchor_median / cls.OUTLIER_RATIO_LOW):
                candidate = val * scale_factor
                z_corrected = cls._calculate_log_z_score(candidate, anchor_median)
                
                # Z-Score Gatekeeper: The correction MUST mathematically improve the distance to the median
                if z_corrected >= z_original:
                    continue
                    
                if (anchor_median / cls.CORRECTION_RANGE) <= candidate <= (anchor_median * cls.CORRECTION_RANGE):
                    corrected_series.at[idx] = candidate
                    correction_flags.at[idx] = True
                    corrections_count += 1
                    
            elif val > (anchor_median * cls.OUTLIER_RATIO_HIGH) and can_scale_down:
                candidate = val / scale_factor
                z_corrected = cls._calculate_log_z_score(candidate, anchor_median)
                
                # Z-Score Gatekeeper
                if z_corrected >= z_original:
                    continue
                    
                if (anchor_median / cls.CORRECTION_RANGE) <= candidate <= (anchor_median * cls.CORRECTION_RANGE):
                    corrected_series.at[idx] = candidate
                    correction_flags.at[idx] = True
                    corrections_count += 1

        top_currency = currency_series.mode()[0] if not currency_series.empty else "UNKNOWN"
        report = ColumnScaleReport(
            column_name=series.name or "unknown",
            total_processed=len(valid_data),
            total_corrected=corrections_count,
            median_anchor=anchor_median,
            inferred_currency=str(top_currency)
        )
        return corrected_series, correction_flags, report
