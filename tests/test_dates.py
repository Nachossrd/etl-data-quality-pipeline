import pytest
import pandas as pd
from rules.dates import parse_dates_robust

def test_parse_dates():
    s = pd.Series([
        "2026-04-01", 
        "01/04/2026", 
        "Apr 01 2026", 
        "2026/04/01", 
        "06-04-2026", 
        "invalid_date"
    ])
    
    parsed, invalid_mask = parse_dates_robust(s)
    
    # 2026-04-01
    assert parsed[0].year == 2026 and parsed[0].month == 4 and parsed[0].day == 1
    # 01/04/2026
    assert parsed[1].year == 2026 and parsed[1].month == 4 and parsed[1].day == 1
    # Apr 01 2026
    assert parsed[2].year == 2026 and parsed[2].month == 4 and parsed[2].day == 1
    # 2026/04/01
    assert parsed[3].year == 2026 and parsed[3].month == 4 and parsed[3].day == 1
    # 06-04-2026 (dayfirst)
    assert parsed[4].year == 2026 and parsed[4].month == 4 and parsed[4].day == 6
    
    assert invalid_mask[5] == True
    assert pd.isna(parsed[5])
