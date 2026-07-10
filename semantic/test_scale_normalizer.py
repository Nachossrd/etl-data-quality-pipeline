import pytest
import pandas as pd
import numpy as np
from semantic.column_scale_normalizer import ColumnScaleNormalizer, CurrencyInferenceEngine
from semantic.integration_fix import normalize_monetary_column_full

def test_bug_850_asus_laptop():
    # Context implies values around ~1M. 850 is a clear outlier missing its 'k' suffix.
    s = pd.Series([1200000.0, 950000.0, 850.0, 1500000.0])
    corrected, flags, report = ColumnScaleNormalizer.normalize_series(s)
    
    assert corrected.iloc[2] == 850000.0
    assert flags.iloc[2] == True
    assert report.total_corrected == 1

def test_bug_inflation_sesenta_lucas():
    # Context implies values around ~50k. 60,000,000 is an outlier (extra zeroes).
    s = pd.Series([45000.0, 60000000.0, 50000.0, 55000.0])
    corrected, flags, report = ColumnScaleNormalizer.normalize_series(s)
    
    assert corrected.iloc[1] == 60000.0
    assert flags.iloc[1] == True
    assert report.total_corrected == 1

def test_bayesian_currency_inference():
    s = pd.Series(["CLP", None, None, None, None])
    inferred = CurrencyInferenceEngine.infer_currency(s)
    
    assert (inferred == "CLP").all()
    assert len(inferred) == 5

def test_edge_case_low_domain():
    # If the domain is normally low, 850 should NOT be corrected.
    s = pd.Series([800.0, 900.0, 850.0, 1000.0])
    corrected, flags, report = ColumnScaleNormalizer.normalize_series(s)
    
    assert corrected.iloc[2] == 850.0
    assert flags.iloc[2] == False
    assert report.total_corrected == 0

def test_integration_fix_cleanup():
    # Ensure columns ending in _sucio are dropped and all final columns are created.
    df = pd.DataFrame({
        "precio": ["1.200.000 CLP", "950k", "850", "1.500.000"],
        "precio_sucio": [1, 2, 3, 4]
    })
    
    df_out = normalize_monetary_column_full(df, "precio")
    
    assert "precio_sucio" not in df_out.columns
    assert "precio_parsed" in df_out.columns
    assert "precio_final" in df_out.columns
    assert "precio_final_currency" in df_out.columns
    assert "precio_scale_corrected" in df_out.columns
    
    assert df_out["precio_final"].iloc[2] == 850000.0
    assert df_out["precio_final_currency"].iloc[2] == "CLP"
