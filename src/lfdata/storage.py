"""Capas de almacenamiento: cruda (raw/) y curada (curated/), ADR 0003.

El destino es una URI base: ``file://./data`` en desarrollo,
``s3://lfdata-data-593760774245`` en producción. La elección es solo
configuración (``--data`` o ``$LFDATA_DATA``); la misma ingesta escribe a un
sitio u otro sin cambios de código.

Las dos capas operan exclusivamente contra :class:`StorageBackend`, un
protocolo de cuatro operaciones (``write_bytes``, ``read_bytes``, ``exists``,
``list_keys``). Hay dos implementaciones —:class:`LocalBackend` y
:class:`S3Backend`— y ninguna store conoce cuál tiene debajo.
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


class S3Backend:
    """Guarda y lee bytes bajo un bucket S3 (y un prefijo opcional).

    Una PUT de S3 es atómica por sí misma —un lector ve el objeto anterior o el
    nuevo completo, nunca uno a medias—, así que :meth:`write_bytes` no necesita
    el baile de fichero temporal del backend local.

    Las credenciales y la región las resuelve boto3 por su cadena estándar
    (perfil ``AWS_PROFILE``, variables de entorno, rol de la tarea...): este
    backend nunca las codifica. En tests se le inyecta un cliente ya construido.
    """

    def __init__(self, bucket: str, prefix: str = "", *, client: object | None = None) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._client = client

    @property
    def _s3(self) -> object:
        if self._client is None:
            import boto3

            self._client = boto3.client("s3")
        return self._client

    def _object_key(self, key: str) -> str:
        """Antepone el prefijo del bucket a la clave lógica de la store."""
        return f"{self._prefix}/{key}" if self._prefix else key

    def write_bytes(self, key: str, payload: bytes) -> None:
        self._s3.put_object(Bucket=self._bucket, Key=self._object_key(key), Body=payload)

    def read_bytes(self, key: str) -> bytes:
        obj = self._s3.get_object(Bucket=self._bucket, Key=self._object_key(key))
        return obj["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._object_key(key))
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
                return False
            raise
        return True

    def list_keys(self, prefix: str) -> list[str]:
        object_prefix = self._object_key(prefix)
        strip = len(self._prefix) + 1 if self._prefix else 0
        paginator = self._s3.get_paginator("list_objects_v2")
        keys = [
            item["Key"][strip:]
            for page in paginator.paginate(Bucket=self._bucket, Prefix=object_prefix)
            for item in page.get("Contents", [])
        ]
        return sorted(keys)


def backend_from_uri(base_uri: str) -> StorageBackend:
    if base_uri.startswith("file://"):
        return LocalBackend(Path(base_uri.removeprefix("file://")))
    if base_uri.startswith("s3://"):
        bucket, _, prefix = base_uri.removeprefix("s3://").partition("/")
        if not bucket:
            raise ValueError(f"URI S3 sin bucket: {base_uri!r}")
        return S3Backend(bucket, prefix)
    raise ValueError(f"URI de almacenamiento no soportada: {base_uri!r}")


def _partition_date(segment: str) -> date | None:
    """Fecha de una partición ``fecha_descarga=YYYY-MM-DD``, o ``None`` si no lo es."""
    try:
        return date.fromisoformat(segment.removeprefix("fecha_descarga="))
    except ValueError:
        return None


class RawStore:
    """Respuestas de las fuentes tal cual llegan, antes de interpretarlas.

    Descubrir la descarga más reciente de un fichero exige enumerar el prefijo
    ``raw/{source}/{dataset}/`` entero, y curar una tabla desde raw lo repite una
    vez por jugador y por dataset. Como el prefijo no cambia dentro de un run
    (salvo por lo que la propia store escribe), :meth:`_list_dataset` cachea el
    listado por ``(source, dataset)``: seis enumeraciones por run en vez de seis
    por jugador (issue #72). :meth:`save` mantiene el caché en sync para que un
    fichero recién escrito sea visible sin re-listar; el caché solo vive lo que
    dura el proceso, que es el único que escribe en ese prefijo.
    """

    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend
        self._listing: dict[tuple[str, str], list[str]] = {}

    def _list_dataset(self, source: str, dataset: str) -> list[str]:
        """Claves bajo ``raw/{source}/{dataset}/``, cacheadas por dataset."""
        cache_key = (source, dataset)
        cached = self._listing.get(cache_key)
        if cached is None:
            cached = self._backend.list_keys(f"raw/{source}/{dataset}/")
            self._listing[cache_key] = cached
        return cached

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
        # Mantén el caché en sync solo si ya está poblado: una entrada presente
        # representa el listado completo del prefijo, y debe seguir haciéndolo.
        cached = self._listing.get((source, dataset))
        if cached is not None and key not in cached:
            cached.append(key)
        return key

    def last_download_date(
        self, source: str, dataset: str, name: str, *, extension: str = "json"
    ) -> date | None:
        """Fecha de descarga más reciente guardada para ``name``, o ``None``.

        Recorre las particiones ``fecha_descarga=YYYY-MM-DD`` del dataset y se
        queda con la más nueva que contenga el fichero pedido. Sirve para saltar
        en un backfill lo que ya se descargó hace poco (``--since-days``).
        """
        return self._last_download(source, dataset, name, extension)[0]

    def iter_latest(self, source: str, dataset: str, *, extension: str = "json"):
        """``(name, payload)`` de la descarga más reciente de **cada** nombre del dataset.

        Recorre las particiones ``fecha_descarga=…`` una sola vez y, por cada
        nombre de fichero, se queda con la fecha más nueva. Sirve para reconstruir
        una tabla curada desde todo el raw de un dataset (p. ej. el catálogo de
        SofaScore) sin volver a pedir nada (ADR 0003).
        """
        prefix = f"raw/{source}/{dataset}/"
        suffix = f".{extension}"
        latest: dict[str, tuple[date, str]] = {}
        for key in self._list_dataset(source, dataset):
            if not key.endswith(suffix):
                continue
            rest = key[len(prefix) :]
            partition, _, filename = rest.partition("/")
            if not filename:
                continue
            name = filename[: -len(suffix)]
            download_date = _partition_date(partition)
            if download_date is None:
                continue
            if name not in latest or download_date > latest[name][0]:
                latest[name] = (download_date, key)
        for name in sorted(latest):
            yield name, self._backend.read_bytes(latest[name][1])

    def read_latest(
        self, source: str, dataset: str, name: str, *, extension: str = "json"
    ) -> bytes | None:
        """Payload de la descarga más reciente de ``name``, o ``None`` si no hay.

        Permite volver a curar sin volver a pedir: raw/ es la fuente de verdad
        inmutable, así que una tabla curada perdida o incompleta se reconstruye
        desde aquí en vez de re-scrapear (ADR 0003).
        """
        download_date, key = self._last_download(source, dataset, name, extension)
        if download_date is None or key is None:
            return None
        return self._backend.read_bytes(key)

    def _last_download(
        self, source: str, dataset: str, name: str, extension: str
    ) -> tuple[date | None, str | None]:
        """Fecha y clave de la descarga más reciente de ``name``."""
        prefix = f"raw/{source}/{dataset}/"
        suffix = f"/{name}.{extension}"
        found: list[tuple[date, str]] = []
        for key in self._list_dataset(source, dataset):
            if not key.endswith(suffix):
                continue
            partition = key[len(prefix) :].split("/", 1)[0]
            download_date = _partition_date(partition)
            if download_date is not None:
                found.append((download_date, key))
        if not found:
            return None, None
        return max(found, key=lambda item: item[0])


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

    def distinct_values(
        self,
        table: str,
        column: str,
        *,
        partition: Mapping[str, str] | None = None,
    ) -> set:
        """Valores distintos de ``column`` en una partición, o conjunto vacío.

        Lee solo esa partición (no la tabla entera) y descarta nulos. Sobre una
        partición inexistente devuelve ``set()``. Sirve para saber qué claves ya
        están escritas y saltarlas al reanudar un backfill.
        """
        table_key = self._table_key(table, partition)
        if not self._backend.exists(table_key):
            return set()
        existing = pd.read_parquet(io.BytesIO(self._backend.read_bytes(table_key)))
        return set(existing[column].dropna())

    def read_partition(
        self,
        table: str,
        *,
        partition: Mapping[str, str] | None = None,
    ) -> pd.DataFrame:
        """Lee una única partición, o un DataFrame vacío si no existe.

        A diferencia de :meth:`read_table` no recorre la tabla entera ni añade
        las columnas de partición: devuelve el fichero tal cual. Sirve para
        consultar el estado previo de una partición antes de reescribirla.
        """
        table_key = self._table_key(table, partition)
        if not self._backend.exists(table_key):
            return pd.DataFrame()
        return pd.read_parquet(io.BytesIO(self._backend.read_bytes(table_key)))

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
