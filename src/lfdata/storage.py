"""Capas de almacenamiento: cruda (raw/) y curada (curated/), ADR 0003.

El destino es una URI base: ``file://./data`` en desarrollo,
``s3://...`` en producción (backend S3 pendiente, issue #5).
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd


class LocalBackend:
    """Guarda y lee bytes bajo un directorio raíz del sistema de ficheros."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write_bytes(self, key: str, payload: bytes) -> Path:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def read_bytes(self, key: str) -> bytes:
        return (self.root / key).read_bytes()


def backend_from_uri(base_uri: str) -> LocalBackend:
    if base_uri.startswith("file://"):
        return LocalBackend(Path(base_uri.removeprefix("file://")))
    if base_uri.startswith("s3://"):
        raise NotImplementedError("El backend S3 llega con el issue #5; usa file:// por ahora")
    raise ValueError(f"URI de almacenamiento no soportada: {base_uri!r}")


class RawStore:
    """Respuestas de las fuentes tal cual llegan, antes de interpretarlas."""

    def __init__(self, backend: LocalBackend) -> None:
        self._backend = backend

    def save(
        self,
        source: str,
        dataset: str,
        name: str,
        payload: bytes,
        *,
        extension: str = "json",
        download_date: date | None = None,
    ) -> str:
        download_date = download_date or datetime.now(tz=UTC).date()
        key = (
            f"raw/{source}/{dataset}/fecha_descarga={download_date.isoformat()}/{name}.{extension}"
        )
        self._backend.write_bytes(key, payload)
        return key


class CuratedStore:
    """Tablas Parquet por nombre, con particiones opcionales estilo Hive."""

    def __init__(self, backend: LocalBackend) -> None:
        self._backend = backend

    def write_table(
        self,
        table: str,
        df: pd.DataFrame,
        *,
        partition: Mapping[str, str] | None = None,
    ) -> str:
        if partition:
            parts = "/".join(f"{key}={value}" for key, value in partition.items())
            key = f"curated/{table}/{parts}/data.parquet"
            # Estilo Hive: la columna particionada vive en la ruta, no en el fichero.
            df = df.drop(columns=[col for col in partition if col in df.columns])
        else:
            key = f"curated/{table}.parquet"
        buffer = io.BytesIO()
        df.to_parquet(buffer, index=False)
        self._backend.write_bytes(key, buffer.getvalue())
        return key

    def read_table(self, table: str) -> pd.DataFrame:
        """Lee la tabla completa, incluidas todas sus particiones."""
        root = self._backend.root / "curated"
        single = root / f"{table}.parquet"
        if single.exists():
            return pd.read_parquet(single)
        return pd.read_parquet(root / table)


class Storage:
    """Punto de entrada único: las dos capas sobre el mismo backend."""

    def __init__(self, base_uri: str) -> None:
        self.base_uri = base_uri
        backend = backend_from_uri(base_uri)
        self.raw = RawStore(backend)
        self.curated = CuratedStore(backend)
