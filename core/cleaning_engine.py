"""Facade del CleaningEngine: delega al cleaner adecuado del registry.

Compatibilidad: mantiene la firma original `CleaningEngine.clean_chunk(df)`
para no romper código que la llama. Internamente selecciona el cleaner por
auto-detección. Para forzar un cleaner específico, usar la nueva firma
`clean_chunk(df, cleaner_name=...)` o pasar `hint=...`.

La lógica histórica (transactional_es) se movió a
`core/cleaners/transactional_es.py`. Otros cleaners conviven en el mismo
package: car_prices, imdb_title_basics, imdb_title_ratings.
"""

from typing import Optional, Type

import pandas as pd

from core.cleaners import REGISTRY, Cleaner
from core.logging_engine import setup_logger

logger = setup_logger("cleaning_engine")


class CleaningEngine:
    """Facade compatible con la API legacy."""

    @staticmethod
    def select_cleaner(df: pd.DataFrame,
                       cleaner_name: Optional[str] = None,
                       hint: Optional[str] = None) -> Optional[Type[Cleaner]]:
        """Resuelve qué cleaner aplica, sin limpiar nada.

        Expuesto aparte de `clean_chunk` porque el orquestador necesita la
        *clase* — no sólo el DataFrame limpio — para leer lo que el cleaner
        declara sobre sí mismo (`semantic_options`, `rules_dataset`).
        """
        if cleaner_name:
            cleaner_cls = REGISTRY.get(cleaner_name)
            if cleaner_cls is not None:
                return cleaner_cls
            logger.warning(f"cleaner_name='{cleaner_name}' no registrado, "
                            "se intentará auto-detección")
        return REGISTRY.auto_detect(df, hint=hint)

    @staticmethod
    def clean_chunk(df: pd.DataFrame,
                    cleaner_name: Optional[str] = None,
                    hint: Optional[str] = None) -> pd.DataFrame:
        """Aplica el cleaner adecuado a un chunk.

        Args:
            df: chunk a limpiar.
            cleaner_name: forzar un cleaner específico por nombre. Si no existe,
                cae a auto-detección. Útil cuando el orquestador conoce la tabla.
            hint: nombre de archivo o tabla; se usa para auto-detección parcial.
        """
        if df is None or df.empty:
            return df

        cleaner_cls = CleaningEngine.select_cleaner(df, cleaner_name, hint)

        if cleaner_cls is None:
            logger.warning("Ningún cleaner aplicable; chunk devuelto sin cambios")
            return df

        logger.info(f"Cleaner seleccionado: {cleaner_cls.name}")
        return cleaner_cls.clean_chunk(df)
