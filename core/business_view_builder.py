import re
import pandas as pd
from core.logging_engine import setup_logger
from typing import Tuple

logger = setup_logger("business_view_builder")

class BusinessViewBuilder:
    @staticmethod
    def build_view(df: pd.DataFrame, domain_col: str = None, strict_columns: list = None, base_currency: str = 'CLP', classifications: dict = None) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Refactors the presentation layer into a clean, business-friendly report.
        Returns:
            Tuple[pd.DataFrame, pd.DataFrame]: (df_legit, df_foreign)
        """
        if df.empty:
            return df.copy(), df.copy()

        df_biz = df.copy()
        protected_keys = BusinessViewBuilder._get_protected_keys(df_biz, classifications)

        # Find domain column
        if domain_col is None:
            domain_col = next((c for c in df_biz.columns if str(c).endswith('_domain_flag')), None)

        # 4. Imputación Relacional (Auto-Secuencia de IDs) - Must happen before tech cleanup and dropping
        df_biz = BusinessViewBuilder._impute_missing_ids(df_biz)

        # Find monetary columns for rescue rule
        monetary_cols = [c for c in df_biz.columns if any(k in str(c).lower() for k in ['precio', 'monto', 'total', 'valor'])]

        # --- 5. Rescatar la Fuga de Inventario ---
        if domain_col and domain_col in df_biz.columns:
            domain_values = df_biz[domain_col].astype(str).str.lower()
            foreign_mask = domain_values.isin(['foreign', 'unlikely'])
            
            legit_domain_mask = ~foreign_mask
            
            has_money_mask = pd.Series(False, index=df_biz.index)
            for mc in monetary_cols:
                has_money_mask = has_money_mask | (pd.to_numeric(df_biz[mc], errors='coerce').fillna(0) > 0)
            
            df_legit_mask = legit_domain_mask & has_money_mask
            df_foreign_mask = foreign_mask
        else:
            df_legit_mask = pd.Series(True, index=df_biz.index)
            df_foreign_mask = pd.Series(False, index=df_biz.index)

        df_legit = df_biz[df_legit_mask].copy()
        df_foreign = df_biz[df_foreign_mask].copy()

        # Apply column cleanup to both dataframes
        df_legit = BusinessViewBuilder._clean_columns(df_legit, strict_columns, base_currency, protected_keys)
        df_foreign = BusinessViewBuilder._clean_columns(df_foreign, strict_columns, base_currency, protected_keys)

        return df_legit, df_foreign

    @staticmethod
    def _get_protected_keys(df: pd.DataFrame, classifications: dict = None) -> list:
        protected = []
        if classifications:
            for col, meta in classifications.items():
                role = getattr(meta, 'role', meta.get('role', None) if isinstance(meta, dict) else None)
                if str(role).endswith('KEY'):
                    protected.append(col)
        
        if not protected:
            key_pattern = re.compile(r'^(id_|_id|fk_|pk_)|(_id|id_|fk_|pk_)$|^(id|codigo|uuid|key)$', re.IGNORECASE)
            for col in df.columns:
                if key_pattern.search(str(col)):
                    protected.append(col)
                    
        return protected

    @staticmethod
    def _impute_missing_ids(df: pd.DataFrame) -> pd.DataFrame:
        """
        Auto-generates sequential IDs for missing or NaN values in the ID column.
        """
        id_col = None
        for col in df.columns:
            if any(k in str(col).lower() for k in ['id_', 'id', 'codigo', 'folio', 'identificador']):
                id_col = col
                break
                
        if id_col is None:
            return df

        # Only process if there are actual missing values
        missing_mask = df[id_col].isna() | (df[id_col] == '') | (df[id_col].astype(str).str.strip() == '')
        if not missing_mask.any():
            return df

        valid_ids = df.loc[~missing_mask, id_col].astype(str)
        
        prefix = "AUTO-"
        padding = 4
        current_max = 0
        
        # Extract pattern from existing IDs if available
        if not valid_ids.empty:
            pattern = re.compile(r'^(.*?)(\d+)$')
            max_val = -1
            best_prefix = "AUTO-"
            best_pad = 4
            
            for val in valid_ids:
                match = pattern.match(val)
                if match:
                    p, num_str = match.groups()
                    num = int(num_str)
                    if num > max_val:
                        max_val = num
                        best_prefix = p
                        best_pad = len(num_str)
            
            if max_val != -1:
                prefix = best_prefix
                padding = best_pad
                current_max = max_val

        # Fill missing IDs sequentially
        for idx in df[missing_mask].index:
            current_max += 1
            new_id = f"{prefix}{str(current_max).zfill(padding)}"
            df.at[idx, id_col] = new_id

        return df

    @staticmethod
    def _consolidate_currencies(df: pd.DataFrame, is_strict: bool = False, base_currency: str = 'CLP') -> pd.DataFrame:
        """
        Currency stutter annihilator.

        - If 100% of detected currency values equal base_currency (or are empty),
          every column containing 'moneda'/'currency' (any prefix) is dropped
          absolutely. The base currency is implied by country context.
        - If 100% of values are a single non-base currency, collapse into one
          'Moneda' column (unless strict mode is on, in which case drop them too).
        - If multiple currencies coexist (FX failure), leave the originals alone.
        """
        currency_cols = [c for c in df.columns if 'moneda' in str(c).lower() or 'currency' in str(c).lower()]
        if not currency_cols:
            return df

        # Valores "no-moneda": ruido del detector (DESCONOCIDA, NaN string,
        # vacíos) que NO deben contar como una moneda distinta para el set.
        noise_tokens = {'', 'UNKNOWN', 'DESCONOCIDA', 'DESCONOCIDO', 'NA', 'NAN', 'NONE', 'NULL', '-'}

        unique_currencies = set()
        for col in currency_cols:
            uniques = df[col].dropna().astype(str).str.strip().str.upper().unique()
            unique_currencies.update(uniques)

        unique_currencies = {u for u in unique_currencies if u and u not in noise_tokens}
        base_upper = str(base_currency).strip().upper()

        # Letal: 100% moneda base (o todo vacío). Drop absoluto, sin Moneda residual.
        if not unique_currencies or unique_currencies == {base_upper}:
            df = df.drop(columns=currency_cols, errors='ignore')
            return df

        # Una sola moneda extranjera homogénea: colapsar a 'Moneda' (salvo strict).
        if len(unique_currencies) == 1:
            global_currency = next(iter(unique_currencies))
            df = df.drop(columns=currency_cols, errors='ignore')
            if not is_strict:
                df['Moneda'] = global_currency
            return df

        # Múltiples monedas: el FX falló o no se llamó. No tocar.
        return df

    @staticmethod
    def _clean_columns(df: pd.DataFrame, strict_columns: list = None, base_currency: str = 'CLP', protected_keys: list = None) -> pd.DataFrame:
        if df.empty:
            return df

        if protected_keys is None:
            protected_keys = []

        # --- 3. Exterminio Quirúrgico del Tech-Bloat ---
        # Sólo matchea columnas que siguen la nomenclatura EXACTA del motor
        # forense. Nombres genéricos de negocio como `meta_score`, `nota_curso`
        # o `user_review_score` sobreviven porque no llevan los anclajes.
        #
        # Patrones eliminados:
        #   - Empieza con: quality_score, data_quality_
        #   - Termina en : _confidence, _scale_corrected, _is_serial_date,
        #                  _domain_flag, _parse_error, _raw,
        #                  _monto_origen, _moneda_origen (auditoría FX)
        tech_pattern = re.compile(
            r'^(quality_score|data_quality_)'
            r'|(_confidence|_scale_corrected|_is_serial_date|_domain_flag|_parse_error|_raw|_monto_origen|_moneda_origen)$',
            re.IGNORECASE
        )
        cols_to_drop = [c for c in df.columns if tech_pattern.search(str(c)) and c not in protected_keys]
        df = df.drop(columns=cols_to_drop, errors='ignore')

        # --- 2. Regla Highlander Todo-Terreno (Limpieza de Raíz Léxica) ---
        hierarchy = ['_semantic', '_final', '_normalized', '_parsed', '_as_date']
        
        # We also need to strip plain text suffixes that might come from previous operations
        def get_base_name(col_name: str) -> str:
            base = str(col_name)
            # Remove technical underscores first
            for suffix in hierarchy:
                if base.endswith(suffix):
                    base = base[:-len(suffix)]
            # Then remove plain text trailing words (case insensitive)
            base = re.sub(r'(?i)[_\s]+(normalizado|original|limpio|final|semantic|parsed)$', '', base)
            return base.strip()

        base_groups = {}
        for col in df.columns:
            base = get_base_name(col)
            if base not in base_groups:
                base_groups[base] = []
            base_groups[base].append(str(col))

        cols_to_keep = []
        rename_map = {}

        for base_name, group_cols in base_groups.items():
            if len(group_cols) == 1:
                col = group_cols[0]
                cols_to_keep.append(col)
                rename_map[col] = col if col in protected_keys else base_name
                continue

            # Highlander fight: find the highest hierarchy score
            best_col = group_cols[0]
            best_score = -1
            is_protected = False
            
            for col in group_cols:
                if col in protected_keys:
                    best_col = col
                    best_score = 999
                    is_protected = True
                    break
                
                score = 0
                col_lower = str(col).lower()
                for i, suffix in enumerate(reversed(hierarchy)):
                    # Check both technical suffix and plain text suffix
                    plain_suffix = suffix.replace('_', ' ').strip()
                    if col_lower.endswith(suffix) or col_lower.endswith(plain_suffix):
                        score = i + 1
                        break
                
                if score > best_score:
                    best_score = score
                    best_col = col
            
            cols_to_keep.append(best_col)
            rename_map[best_col] = best_col if is_protected else base_name

        df = df[cols_to_keep]
        df = df.rename(columns=rename_map)
        
        # --- Cura para las 'Columnas Tartamudas' y Vacías ---
        df = df.dropna(axis=1, how='all')

        final_rename_map = {}
        for col in df.columns:
            if col in protected_keys:
                final_rename_map[col] = col
                continue
                
            new_name = str(col)
            
            if new_name.endswith('_currency'):
                new_name = new_name.replace('_currency', '_moneda')
            
            new_name = new_name.replace('precio_sucio', 'precio')
            new_name = new_name.replace('monto_sucio', 'monto')
            new_name = new_name.replace('_sucio', '')
            
            new_name = new_name.replace('_', ' ').title().strip()
            
            # Remove consecutive duplicate words
            words = new_name.split()
            clean_words = []
            for w in words:
                if not clean_words or w.lower() != clean_words[-1].lower():
                    clean_words.append(w)
            
            final_name = " ".join(clean_words)
            final_rename_map[col] = final_name

        df = df.rename(columns=final_rename_map)

        # --- Colapsador de Monedas (Consolidación Cambiaria) ---
        df = BusinessViewBuilder._consolidate_currencies(df, is_strict=bool(strict_columns), base_currency=base_currency)

        # --- El Formato de Hora 'Boomer' ---
        for col in df.columns:
            col_lower = str(col).lower()
            is_date = 'fecha' in col_lower or 'date' in col_lower or pd.api.types.is_datetime64_any_dtype(df[col])
            
            if is_date:
                try:
                    df[col] = pd.to_datetime(df[col], errors='coerce').dt.strftime('%Y-%m-%d')
                except Exception:
                    pass

        # --- El Machete Estricto Inteligente (Smart Strict Schema) ---
        if strict_columns is not None:
            final_columns = strict_columns.copy()
            for k in protected_keys:
                if k in df.columns and k not in final_columns:
                    final_columns.append(k)
            # Reindex to force exactly these columns, filling missing ones with NaNs
            df = df.reindex(columns=final_columns)

        return df
