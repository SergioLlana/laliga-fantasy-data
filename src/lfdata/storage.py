"""Capas de almacenamiento: cruda (raw/) y curada (curated/), ADR 0003.

El destino es una URI base: ``file://./data`` en desarrollo,
``s3://...`` en producción (backend S3 pendiente, issue #5).

Las dos capas operan exclusivamente contra :class:`StorageBackend`, un
protocolo de cuatro operaciones (``write_bytes``, ``read_bytes``, ``exists``,
``list_keys``) que un backend S3 puede implementar tal cual. Ninguna store
conoce la naturaleza de sistema de ficheros del backend local.
"""

from __future__ import annotations

import io
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd


@runtime_checkable
class StorageBackend(Protocol):
    """Almacén de bytes direccionado por clave, agnóstico del medio.

    Una clave es una ruta relativa estilo POSIX (``raw/.../x.json``). Un prefijo
    es una clave parcial; ``list_keys`` enumera todo lo que cuelga de él.
    """

    def write_bytes(self, key: str, payload: bytes) -> None: ...

    def read_bytes(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def list_keys(self, prefix: str) -> list[str]: ...


class LocalBackend:
    """Guarda y lee bytes bajo un directorio raíz del sistema de ficheros."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write_bytes(self, key: str, payload: bytes) -> None:
        """Escritura atómica: fichero temporal en el mismo directorio + rename.

        Un proceso interrumpido a media escritura no deja un Parquet corrupto en
        la partición: hasta el ``os.replace`` final la clave conserva su valor
        anterior (o no existe), y el temporal se limpia si algo falla.
        """
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(tmp_name, path)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise

    def read_bytes(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()

    def list_keys(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if not base.exists():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file())


def backend_from_uri(base_uri: str) -> StorageBackend:
    if base_uri.startswith("file://"):
        return LocalBackend(Path(base_uri.removeprefix("file://")))
    if base_uri.startswith("s3://"):
        raise NotImplementedError("El backend S3 llega con el issue #5; usa file:// por ahora")
    raise ValueError(f"URI de almacenamiento no soportada: {base_uri!r}")


class RawStore:
    """Respuestas de las fuentes tal cual llegan, antes de interpretarlas."""

    def __init__(self, backend: StorageBackend) -> None:
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
        prefix = f"raw/{source}/{dataset}/"
        suffix = f"/{name}.{extension}"
        dates: list[date] = []
        for key in self._backend.list_keys(prefix):
            if not key.endswith(suffix):
                continue
            partition = key[len(prefix) :].split("/", 1)[0]
            try:
                dates.append(date.fromisoformat(partition.removeprefix("fecha_descarga=")))
            except ValueError:
                continue
        return max(dates) if dates else None


class CuratedStore:
    """Tablas Parquet por nombre, con particiones opcionales estilo Hive."""

    def __init__(self, backend: StorageBackend) -> None:
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
        table_key = self._table_key(table, partition)
        incoming = df.drop(columns=[col for col in (partition or {}) if col in df.columns])
        if self._backend.exists(table_key):
            existing = pd.read_parquet(io.BytesIO(self._backend.read_bytes(table_key)))
            kept = existing[~existing[key].isin(set(incoming[key]))]
            combined = pd.concat([kept, incoming], ignore_index=True)
        else:
            combined = incoming
        return self.write_table(table, combined, partition=partition)

    def retain_keys(
        self,
        table: str,
        keep: set,
        *,
        key: str = "id",
        partition: Mapping[str, str] | None = None,
    ) -> int:
        """Reescribe la partición conservando solo las filas cuyo ``key`` esté en ``keep``.

        Sirve al refresh completo: tras recorrer la competición entera, retira de
        la tabla a quien ya no aparece en ninguna plantilla. Devuelve cuántas
        filas se eliminaron; sobre una partición inexistente no hace nada.
        """
        table_key = self._table_key(table, partition)
        if not self._backend.exists(table_key):
            return 0
        existing = pd.read_parquet(io.BytesIO(self._backend.read_bytes(table_key)))
        kept = existing[existing[key].isin(keep)]
        removed = len(existing) - len(kept)
        if removed:
            self.write_table(table, kept, partition=partition)
        return removed

    @staticmethod
    def _table_key(table: str, partition: Mapping[str, str] | None) -> str:
        if partition:
            parts = "/".join(f"{name}={value}" for name, value in partition.items())
            return f"curated/{table}/{parts}/data.parquet"
        return f"curated/{table}.parquet"

    def read_table(self, table: str) -> pd.DataFrame:
        """Lee la tabla completa, incluidas todas sus particiones.

        Enumera con ``list_keys`` los ``data.parquet`` bajo la tabla, lee cada
        uno y reconstruye las columnas de partición desde la clave (estilo Hive),
        fijando su tipo a ``str`` para no depender de la inferencia de pyarrow.
        """
        single = self._table_key(table, None)
        if self._backend.exists(single):
            return pd.read_parquet(io.BytesIO(self._backend.read_bytes(single)))

        prefix = f"curated/{table}/"
        suffix = "/data.parquet"
        frames: list[pd.DataFrame] = []
        for key in self._backend.list_keys(prefix):
            if not key.endswith(suffix):
                continue
            frame = pd.read_parquet(io.BytesIO(self._backend.read_bytes(key)))
            for segment in key[len(prefix) : -len(suffix)].split("/"):
                name, _, value = segment.partition("=")
                frame[name] = value
            frames.append(frame)
        if not frames:
            raise FileNotFoundError(f"No hay datos curados para la tabla {table!r}")
        return pd.concat(frames, ignore_index=True)


class Storage:
    """Punto de entrada único: las dos capas sobre el mismo backend."""

    def __init__(self, base_uri: str) -> None:
        self.base_uri = base_uri
        backend = backend_from_uri(base_uri)
        self.raw = RawStore(backend)
        self.curated = CuratedStore(backend)
