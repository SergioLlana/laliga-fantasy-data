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

    def last_download_date(
        self, source: str, dataset: str, name: str, *, extension: str = "json"
    ) -> date | None:
        """Fecha de descarga más reciente guardada para ``name``, o ``None``.

        Recorre las particiones ``fecha_descarga=YYYY-MM-DD`` del dataset y se
        queda con la más nueva que contenga el fichero pedido. Sirve para saltar
        en un backfill lo que ya se descargó hace poco (``--since-days``).
        """
        base = self._backend.root / "raw" / source / dataset
        if not base.exists():
            return None
        dates: list[date] = []
        for partition in base.glob("fecha_descarga=*"):
            if not (partition / f"{name}.{extension}").exists():
                continue
            try:
                dates.append(date.fromisoformat(partition.name.removeprefix("fecha_descarga=")))
            except ValueError:
                continue
        return max(dates) if dates else None


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
        """Reescribe la partición entera (refresh completo)."""
        key = self._table_key(table, partition)
        if partition:
            # Estilo Hive: la columna particionada vive en la ruta, no en el fichero.
            df = df.drop(columns=[col for col in partition if col in df.columns])
        buffer = io.BytesIO()
        df.to_parquet(buffer, index=False)
        self._backend.write_bytes(key, buffer.getvalue())
        return key

    def upsert_table(
        self,
        table: str,
        df: pd.DataFrame,
        *,
        key: str = "player_id",
        partition: Mapping[str, str] | None = None,
    ) -> str:
        """Actualiza filas por ``key`` sin tocar al resto de la partición.

        Lee la partición existente, descarta las filas cuyo ``key`` aparece en el
        lote entrante y reescribe con las nuevas. Es idempotente (re-escribir el
        mismo lote deja la tabla igual) y hace la ingesta reanudable: un run
        parcial refresca solo esos ``key`` sin borrar a los demás. Sobre una
        partición vacía equivale a :meth:`write_table`.
        """
        path = self._backend.root / self._table_key(table, partition)
        incoming = df.drop(columns=[col for col in (partition or {}) if col in df.columns])
        if path.exists():
            existing = pd.read_parquet(path)
            kept = existing[~existing[key].isin(set(incoming[key]))]
            combined = pd.concat([kept, incoming], ignore_index=True)
        else:
            combined = incoming
        return self.write_table(table, combined, partition=partition)

    @staticmethod
    def _table_key(table: str, partition: Mapping[str, str] | None) -> str:
        if partition:
            parts = "/".join(f"{name}={value}" for name, value in partition.items())
            return f"curated/{table}/{parts}/data.parquet"
        return f"curated/{table}.parquet"

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
