"""Facade del CleaningEngine: delega al cleaner adecuado del registry.

Compatibilidad: mantiene la firma original `CleaningEngine.clean_chunk(df)`
para no romper código que la llama. Internamente selecciona el cleaner por
auto-detección. Para forzar un cleaner específico, usar la nueva firma
`clean_chunk(df, cleaner_name=...)` o pasar `hint=...`.

La lógica histórica (transactional_es) se movió a
`core/cleaners/transactional_es.py`. Otros cleaners conviven en el mismo
package: car_prices, imdb_title_basics, imdb_title_ratings.
"""

from typing import Optional

import pandas as pd

from core.cleaners import REGISTRY
from core.logging_engine import setup_logger

logger = setup_logger("cleaning_engine")


class CleaningEngine:
    """Facade compatible con la API legacy."""

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

        cleaner_cls = None
        if cleaner_name:
            cleaner_cls = REGISTRY.get(cleaner_name)
            if cleaner_cls is None:
                logger.warning(f"cleaner_name='{cleaner_name}' no registrado, "
                                "se intentará auto-detección")

        if cleaner_cls is None:
            cleaner_cls = REGISTRY.auto_detect(df, hint=hint)

        if cleaner_cls is None:
            logger.warning("Ningún cleaner aplicable; chunk devuelto sin cambios")
            return df

        logger.info(f"Cleaner seleccionado: {cleaner_cls.name}")
        return cleaner_cls.clean_chunk(df)
