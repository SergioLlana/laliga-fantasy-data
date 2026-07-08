# S3 con capa cruda y capa curada

Todos los datos ingeridos se guardan en S3 en dos capas: `raw/` conserva las respuestas de cada fuente tal cual llegan (JSON o HTML, particionadas por fuente y fecha de descarga) y `curated/` contiene tablas Parquet limpias con esquema estable e IDs canónicos. Cualquier bug de parseo o cambio de esquema se corrige reprocesando desde `raw/`, sin volver a scrapear fuentes que bloquean o que pueden haber borrado el histórico.

## Considered Options

- **Solo curado**: más simple, pero re-scrapear es caro (bloqueos) y a veces imposible (datos que desaparecen).
- **Postgres (RDS) como almacén principal**: más cómodo para la web, pero más caro y contradice el requisito de S3 como almacén actualizable.

## Consequences

El pipeline queda dividido en dos pasos con contrato claro: descargar (fuente → raw) y transformar (raw → curated). La web y los modelos solo leen de `curated/`; nada aguas abajo depende del formato de una fuente externa.
