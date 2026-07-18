# La partición por `competition` de las tablas de historial de Transfermarkt es procedencia del crawl, no ámbito del dato

La ingesta de Transfermarkt produce cinco tablas. [ADR 0005](0005-transfermarkt-players-particionada-por-temporada.md) separó `transfermarkt_players` —pertenencia a una plantilla, particionada por `(competition, season)`— de las otras cuatro —el **historial del jugador**: `market_values_tm`, `transfers`, `availability_tm`, `injuries_tm`, particionadas solo por `competition`—. Este ADR precisa qué significa esa partición por `competition` en las cuatro de historial, porque de ella depende poder ingerir un jugador **fuera de plantilla** y leer la tabla entera sin duplicados.

## El dato de historial es de carrera completa

Los cuatro endpoints de historial de Transfermarkt (`marketValueDevelopment`, `transferHistory`, `performance-game`, la página de lesiones) devuelven la **carrera completa** del jugador en todas las competiciones a la vez, no la porción de una liga (verificado en `docs/experiments/2026-07-07-alex-fores.md`: los 232 partidos de Álex Forés en La Liga, Segunda, Primera Federación, Copa y europeas vienen en una sola respuesta). Por eso la columna de partición `competition` de estas tablas **no es el ámbito del dato** —el dato no es «el valor de mercado *en La Liga*»— sino la **procedencia del crawl**: la competición desde cuya plantilla se alcanzó al jugador. El mismo histórico se habría curado idéntico alcanzándolo desde cualquier otra.

De aquí se sigue que un jugador **fuera de plantilla** no necesita fila en `transfermarkt_players` (esa tabla sí es pertenencia a plantilla, ADR 0005): se cura solo su historial. Es lo que permite `lfdata ingest transfermarkt-player`, que alcanza al jugador por su `spieler_id` —los `sin-candidato` enlazados a mano, y el goteo de fichajes de Segunda/extranjero antes de aparecer en el kader— y cura las cuatro tablas de historial, nunca `transfermarkt_players`. Ese jugador aterriza en la partición centinela `competition=bajo-demanda`: no vino de ningún kader, así que su procedencia de crawl es «bajo demanda».

## Un jugador vive en exactamente una partición (invariante de unicidad)

Que la partición sea procedencia y no ámbito abre un riesgo: el mismo jugador puede alcanzarse desde dos competiciones (un cedido que aparece en el kader de La Liga y en el de Segunda; o alguien alcanzado por id y después por un kader), y quedaría **duplicado** —sus mismas filas de carrera bajo dos particiones—. Como `lfdata map` y el resto de consumidores leen la tabla entera (unión de todas las particiones), el duplicado se propagaría.

La regla que lo evita: **cada jugador vive en exactamente una partición de cada tabla de historial**. Al escribir el historial de un jugador en una partición se retiran sus filas de todas las demás (`CuratedStore.upsert_unique_partition`). El jugador alcanzado por id va a `bajo-demanda`; cuando después aparezca en un kader de La Liga, el refresh de ese club lo mueve a `competition=la-liga` retirándolo de `bajo-demanda`. Con la invariante, leer la tabla entera es inocuo: no hay que deduplicar.

## Considered Options

- **Particionar el historial por competición como ámbito** (filtrar cada endpoint a la liga de la partición): imposible sin descartar datos que la fuente sí da —el endpoint es de carrera completa— y además rompería el caso de uso (un fichaje del Brasileirão no tendría dónde vivir hasta jugar en La Liga).
- **No particionar el historial en absoluto** (una tabla plana por jugador): coherente con «el dato es de carrera completa», pero rompería el resto del pipeline, que asume el layout Hive por `competition` de ADR 0005, y perdería la traza de por dónde entró cada jugador. La partición-como-procedencia conserva ese layout y esa traza sin fingir un ámbito que el dato no tiene.
- **Permitir el duplicado y deduplicar al leer**: cada consumidor tendría que recordar deduplicar, y olvidarlo sería un bug silencioso. La invariante de unicidad lo arregla en la escritura, una sola vez.

## Consequences

Extiende (no contradice) ADR 0005: `transfermarkt_players` sigue particionada por `(competition, season)` y sujeta a su poda; las cuatro tablas de historial siguen particionadas solo por `competition`, ahora con la semántica explícita de procedencia y la invariante de unicidad. La ingesta por competición (`_ingest_clubs`) y la ingesta por jugador (`ingest_player`) usan ambas `upsert_unique_partition` para las cuatro tablas, así que el duplicado latente que aparecería al ingerir `segunda-division` sobre un jugador ya alcanzado desde La Liga queda descartado de raíz. `raw/` sigue siendo la única fuente de verdad reprocesable (ADR 0003): `--cached` re-cura el historial desde raw sin una sola petición.
