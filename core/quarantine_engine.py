import pandas as pd
import os
from core.logging_engine import setup_logger
from config.settings import PipelineConfig

logger = setup_logger("quarantine_engine")

class QuarantineEngine:
    @staticmethod
    def process(df: pd.DataFrame, table_name: str) -> pd.DataFrame:
        """
        Evaluates the dataframe and moves corrupt rows to quarantine.
        Returns the clean dataframe.
        """
        if df.empty:
            return df
            
        os.makedirs(PipelineConfig.QUARANTINE_DIR, exist_ok=True)

        # Rules for quarantine: score < 50, or invalid critical IDs.
        # IMPORTANT: si ninguna de las columnas de forensia existe, el
        # quarantine es no-op (no es bug del dataset, simplemente no aplica).
        if "quality_score" in df.columns:
            mask_quarantine = df["quality_score"] < 50
        else:
            mask_quarantine = pd.Series(False, index=df.index)

        if "data_quality_flags" in df.columns:
            id_flag_mask = df["data_quality_flags"].apply(
                lambda flags: bool(flags) and any(
                    "missing_id" in t or "invalid_id" in t
                    for t in (flags if isinstance(flags, list) else [])
                )
            )
            mask_quarantine = mask_quarantine | id_flag_mask
            
        quarantine_df = df[mask_quarantine].copy()
        clean_df = df[~mask_quarantine].copy()
        
        if not quarantine_df.empty:
            q_file = os.path.join(PipelineConfig.QUARANTINE_DIR, f"{table_name}_quarantine.csv")
            # Append if exists
            mode = 'a' if os.path.exists(q_file) else 'w'
            header = not os.path.exists(q_file)
            quarantine_df.to_csv(q_file, mode=mode, header=header, index=False)
            logger.warning(f"Sent {len(quarantine_df)} rows to quarantine for table {table_name}")
            
        return clean_df
