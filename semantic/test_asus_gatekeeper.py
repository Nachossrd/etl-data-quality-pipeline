import pytest
import pandas as pd
from semantic.integration_fix import normalize_monetary_column_full

def test_asus_gatekeeper():
    # Simulate the array: [1200000, 5000, 850, 200, 60000]
    df = pd.DataFrame({
        "producto": ["Notebook Gamer", "Cable USB", "Asus Laptop", "Mouse Pad", "SSD 1TB"],
        "precio": ["1200000", "5000", "850", "200", "60000"]
    })
    
    df_out = normalize_monetary_column_full(df, "precio")
    
    # 1200000 and 5000 must remain intact (no z-score collapse, thanks to the new bounds and strict Z-score check)
    assert df_out["precio_final"].iloc[0] == 1200000.0
    assert df_out["precio_final"].iloc[1] == 5000.0
    
    # 850 must be corrected to 850000
    assert df_out["precio_final"].iloc[2] == 850000.0
    
    # 200 must be corrected to 200000
    assert df_out["precio_final"].iloc[3] == 200000.0
    
    # 60000 must remain intact
    assert df_out["precio_final"].iloc[4] == 60000.0
