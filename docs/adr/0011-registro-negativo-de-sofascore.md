# El `skip` de SofaScore es un registro negativo por canónico, no un canónico solo-Biwenger

La revisión manual de mappings admite dos decisiones: `y` (este candidato es la contraparte) y `skip` (no hay contraparte). En Transfermarkt el `skip` funciona porque es *constitutivo*: crea un **canónico solo-Biwenger** —una fila `(BIWENGER, id)` en `players.csv` sin contraparte de ninguna fuente—, y esa fila persiste, así que en la siguiente pasada el jugador está en `approved` y no se vuelve a proponer.

En SofaScore no hay canónico que crear: la identidad ya la creó Transfermarkt y SofaScore **se cuelga** de ella ([ADR 0001](0001-canonical-player-ids-with-manual-review.md), [mappings/README](../../mappings/README.md)). El `skip` de SofaScore es un hecho distinto: *"esta identidad canónica que ya existe no tiene contraparte en SofaScore"*. Es un **hecho negativo sobre un canónico**, no la creación de uno.

## El bug que esto causaba (#94)

Al no tener dónde anotarse, el `skip` de SofaScore solo "resolvía" en memoria dentro de una pasada (`resolved.add(biw_id)` y nada más). Pero `resolved` se recalcula en cada ejecución de `lfdata map` exclusivamente desde los mapeos existentes (`_biwenger_ids_with_source`, que cuenta como resuelto al Biwenger cuyo canónico tiene una fila `sofascore`). Un `skip` no dejaba ninguna, así que en la pasada siguiente el jugador no estaba en `resolved` y sus candidatos homónimos —de *otras* personas— se re-proponían con la `decision` vacía. Peor: `_preserve_decisions` descartaba la fila `skip` en la misma pasada que la aplicaba (por estar el Biwenger en `resolved` en ese momento), así que la única copia de la decisión moría con ella. El `skip` de SofaScore era, en la práctica, imposible de cerrar.

## Decisión

El hecho negativo se persiste en un fichero propio, **`mappings/sofascore-skips.csv`**, keyed por `canonical_id`:

```
canonical_id,biwenger_name,fecha
p00123,Joaquín Muñoz,2026-07-18
```

Un único fichero cubre jugadores y equipos: el prefijo del `canonical_id` (`p…`/`t…`, [ADR 0001](0001-canonical-player-ids-with-manual-review.md)) ya distingue el tipo. `map` da por resuelto al Biwenger cuyo canónico está en este fichero, igual que al que ya tiene un mapping. La decisión se ancla en el **canónico**, no en el id de Biwenger, porque es un hecho sobre la identidad —coherente con que SofaScore se cuelga del canónico— y no lleva temporada ([ADR 0006](0006-la-identidad-no-tiene-temporada.md)).

La relación de integridad de [ADR 0001](0001-canonical-player-ids-with-manual-review.md) se extiende de "un canónico tiene como máximo un mapping por fuente" a "**un mapping o un skip** por fuente": un canónico con `skip` de SofaScore *y* una fila `sofascore` a la vez es una contradicción que hace fallar el comando al cargar el store.

**Reabrir un skip = borrar su fila del fichero.** Esto materializa el trade-off que [ADR 0006](0006-la-identidad-no-tiene-temporada.md) ya asumió para Transfermarkt ("un skip cierra la puerta por dentro"): si mañana el backfill de SofaScore amplía cobertura y aparece la contraparte real de un canónico skipeado, nadie la re-propondrá sola. Con un fichero dedicado los skips vigentes están en un solo sitio, auditables y reversibles con un `git rm` de la fila; y `--check` avisa si el `sofascore_player_id` real de un canónico skipeado aparece algún día en el eventing curado (`sofascore sin canonical`), que es la señal para revisar la decisión.

## Considered Options

- **Fila centinela en `players.csv`** (`(canonical, sofascore, "")` o `"none"`). Viola de frente la integridad de [ADR 0001](0001-canonical-player-ids-with-manual-review.md): con dos skips, el id de fuente vacío se duplicaría entre canónicos. Y envenena a los muchos consumidores de `players.csv` que no distinguen filas reales de centinelas (`canonical_by_source`/`approved_ids` para cruzar el eventing en la ingesta de SofaScore, el contador de "mapeados" del informe). Excepcionar el centinela debilitaría la invariante justo donde más protege: los ficheros editados a mano.
- **Conservar las filas `skip` en `sofascore-review.csv`** sin descartarlas al regenerar, usando el propio fichero de revisión como persistencia. Funciona, pero muta la semántica "revisión = candidato pendiente" en un ledger permanente, obliga a bifurcar `_preserve_decisions` —compartido con Transfermarkt, donde el `skip` **sí** debe desaparecer de la revisión— y arrastra candidatos congelados como ruido creciente que el revisor debe aprender a ignorar. Menos código, más deuda semántica.

## Consequences

`_preserve_decisions` no se toca: que descarte la fila `skip` en la misma pasada deja de ser un bug y pasa a ser correcto, porque la persistencia ya vive en el fichero de skips —igual que en Transfermarkt—, sin riesgo de regresión en el `skip` de TM. El bug afectaba también a equipos (mismo código compartido), aunque no mordía porque todos los clubes de La Liga existen en SofaScore; la solución los cubre gratis. Los `skip` que ya estaban marcados en `sofascore-review.csv` cuando se implementó el arreglo se promueven solos en la primera pasada, sin migración manual. El informe de `map` añade cuántos canónicos quedan "sin contraparte" para explicar por qué no aparecen ni mapeados ni en revisión.
