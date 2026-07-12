# `transfermarkt_players` se particiona por temporada; las demás tablas de Transfermarkt, no

La ingesta de Transfermarkt produce cinco tablas, y no todas tienen la misma naturaleza. `transfermarkt_players` es **pertenencia a una plantilla**: un jugador está en el Elche *en 2022*, y esa afirmación no dice nada sobre 2026. Las otras cuatro (`market_values_tm`, `transfers`, `availability_tm`, `injuries_tm`) son el **historial del jugador**: el mismo se alcance desde la temporada que se alcance. Por eso `transfermarkt_players` se particiona por `(competition, season)` y las otras cuatro solo por `competition`.

La consecuencia operativa está en la poda. Un refresh completo retira de `transfermarkt_players` a quien ya no aparece en ninguna plantilla, y esa retirada solo tiene sentido **dentro de una temporada**: recorrer las plantillas de 2022 no autoriza a borrar a los jugadores de 2026, a quienes ni se ha mirado. Las otras cuatro tablas no se podan nunca; solo acumulan por `upsert`.

De aquí se sigue una regla que vale para toda ingesta, no solo para esta: **la capa curada se reconstruye siempre desde `raw/`; lo que no se repite es la descarga.** `--since-days` y `--resume` evitan volver a *pedir* a la fuente lo que ya está bajado, pero nunca se saltan el *curado*: el jugador ya descargado se vuelve a parsear desde `raw/` y se hace `upsert` igual. Saltarse el curado equivale a asumir "si está en raw, ya está en curated", y esa suposición no se sostiene: en cuanto una fila desaparece de la tabla, nada la devuelve.

## Considered Options

- **Una sola partición por competición, sin temporada** (lo que hubo hasta el 2026-07-12): el refresh completo de una temporada histórica podaba a los jugadores de la temporada en curso, y `--since-days` impedía después que volvieran —su raw reciente hacía que se les siguiera saltando, así que el agujero era permanente y ninguna reingesta lo cerraba—. `transfermarkt_players` llegó a tener 306 jugadores de los 515 de las plantillas de La Liga, y el mapping a IDs canónicos se quedó en un 44% porque a la mayoría de los jugadores de Biwenger no había contraparte que ofrecerles.
- **Particionar también las otras cuatro tablas por temporada**: duplicaría el historial de cada jugador en cada temporada desde la que se le alcanza, sin ganar nada: el valor de mercado de un jugador en 2021 es el mismo se le ingiera desde la plantilla de 2022 o desde la de 2026.
- **No podar nunca `transfermarkt_players`**: elimina el problema por la vía de no borrar, pero la tabla dejaría de responder a "quiénes son hoy la plantilla", que es justo lo que el mapping necesita para buscar la contraparte de un jugador de Biwenger en su club.

## Consequences

Quien lea la tabla elige temporada: `lfdata map --season` busca la contraparte de Biwenger en las plantillas de la temporada que se le pida (la actual por defecto). Una tabla curada perdida o incompleta se regenera reingiriendo con `--since-days`, que la reconstruye desde `raw/` sin una sola petición a la fuente —así se recuperaron los 515 jugadores de 2026 tras el incidente—. Y `raw/` queda confirmado como la única fuente de verdad reprocesable, que es lo que [ADR 0003](0003-s3-raw-plus-curated-layers.md) prometía y esta ingesta no cumplía.
