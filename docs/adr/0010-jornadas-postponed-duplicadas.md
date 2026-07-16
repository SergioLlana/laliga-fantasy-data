# Los puntos de `rounds` no son la verdad; `fantasy_points` sí, y las jornadas "(postponed)" se descartan

El backfill de `biwenger-rounds` para 2021–2025 dejó `fantasy_round_points` con dos problemas que resultaron ser uno solo mal entendido. Al verificarla salieron miles de filas duplicadas por `(player_id, match_id)` y, al cruzarla contra `fantasy_points` para decidir con qué versión quedarse, apareció que los puntos de las dos tablas apenas coincidían. El segundo hallazgo cambia qué es cada tabla, así que va primero.

## Los dos endpoints de puntos no miden lo mismo

Biwenger sirve los puntos de una temporada por dos vías. El **detalle por jugador** (`players/{comp}/{slug}`, tabla `fantasy_points`) da un `points` por sistema, minutos y nota, pero solo responde para quien sigue en la plantilla. El endpoint de **jornada** (`rounds/{comp}/{id}?score=N`, tabla `fantasy_round_points`) da un `points` escalar por sistema y enumera a *todos* los que puntuaron, incluidos los que ya se fueron ([ADR 0009](0009-identidad-historica-de-rounds-por-temporada.md)).

Se dio por hecho que ambas darían el mismo punto para el mismo jugador-partido-sistema. **No lo dan.** Cruzando las dos tablas en partidos *no* aplazados de 2024/25, coinciden en ~11% de los jugador-jornada, y de forma uniforme en los cinco sistemas —lo que delata que ese 11% son coincidencias numéricas, no aciertos—. Verificado sobre el `raw/` real, jugador a jugador:

- **Los sistemas están bien alineados.** El `?score=N` devuelve `scoreID=N` y los puntos cambian con `N`. No es una permutación de sistemas: para ningún jugador el vector de la jornada bajo un sistema coincide con el del detalle bajo otro. Tampoco es escala, signo ni redondeo: las diferencias son enteros pequeños sin factor común.
- **No es una atribución al partido equivocado.** El vector de puntos que la jornada da a un jugador no aparece en *ningún* partido de su detalle, ni en la jornada original ni en la aplazada.
- **El detalle reconcilia con la ficha; la jornada no.** Sumando los `points` por partido del detalle de un jugador en 2024/25 salen **exactamente** los totales de temporada que Biwenger publica en su ficha (`seasons[].points`) en los cinco sistemas. El endpoint de jornada da otros números que no suman a nada nuestro.

No sabemos qué calcula exactamente el endpoint de jornada, pero está demostrado que **no es el punto canónico del sistema**. Ese solo lo da el detalle.

**Decisión: `fantasy_points` es la única fuente autoritativa de puntos.** `fantasy_round_points` se mantiene por lo que sí hace sin sesgo de supervivencia —enumerar *quién* jugó cada jornada, incluidos los que se fueron, que es de donde salen `biwenger_players_history`/`biwenger_teams_history` ([ADR 0009](0009-identidad-historica-de-rounds-por-temporada.md))—, pero sus columnas `points_*` **no** se usan como puntuación de ningún modelo. Se conservan en la tabla, sin renombrar, para no cambiar el esquema ni el reprocesado; su carácter no-canónico queda documentado aquí y nada en el código las lee (el detector de fichajes solo mira la *presencia* de `player_id`, no el valor).

## Las jornadas "(postponed)" duplicaban cada partido

Con el segundo hallazgo entendido, el primero se desbloquea. Cuando hubo aplazamientos, el catálogo `season.rounds` trae la jornada **dos veces**: `Round 3` y `Round 3 (postponed)`, ambas `status="finished"`, con los mismos partidos, las mismas fechas y el mismo marcador, pero con puntos distintos (los dos no-canónicos). La 2024/25, la de la DANA, tiene ocho copias así.

`ingest_rounds` descubría las jornadas con `status in (None, "finished")`, incluía las dos y, como el upsert es por `round_id`, ambas sobrevivían: cada partido quedaba dos veces en `fantasy_round_points`. **Descartar del descubrimiento las jornadas cuyo `name` contiene `postponed` elimina el 100% de los duplicados en las cinco temporadas** —comprobado: dedup por `(player_id, match_id)` a cero—. Se conserva la **original**, no la aplazada, porque el detalle (`fantasy_points`) reporta esos partidos bajo el `round_id` original: así ambas tablas siguen unibles por `(player_id, round_id)`.

El mismo filtro va en `ingest_reports_delta`, que también recorre `season.rounds` filtrando por `status == "finished"` para detectar jornadas nuevas. Ahí el daño habría sido peor a largo plazo: como `fantasy_points` solo guarda el `round_id` original, la copia postponed nunca figuraría como "procesada" y el delta la vería como jornada nueva en **cada** run, pidiendo su detalle una y otra vez.

El filtro es un único predicado, `_is_postponed`, sobre el `name` del catálogo; se comparte entre ambas funciones.

## Considered Options

- **Dedupar `fantasy_round_points` por `(player_id, match_id)` tras curar, quedándose con la original.** Corrige el síntoma pero no la causa: seguiría pidiendo las cinco peticiones por sistema de la jornada aplazada (gasto de cuota) y el delta seguiría viéndola como nueva cada run. Filtrar en el descubrimiento lo ataja antes de la red.
- **Quitar las columnas `points_*` de `fantasy_round_points`.** Elimina el footgun de columnas con valores erróneos, pero cambia el esquema y el reprocesado sin ganancia funcional —nadie las lee— y con riesgo de romper lecturas futuras que asuman su forma. Se prefirió documentar y dejar el esquema quieto; si algún día estorban, quitarlas es una migración aparte.
- **Fiarse de los puntos de la jornada y corregir el detalle.** Descartado: el detalle es lo que la ficha del jugador muestra y lo que reconcilia con el total de temporada. Es la definición de "puntos" del producto.

## Consequences

Los `raw/` de las jornadas aplazadas ya están descargados y no se borran (son la fuente reprocesable, [ADR 0003](0003-s3-raw-plus-curated-layers.md)); simplemente dejan de curarse. Las cinco particiones de `fantasy_round_points` escritas antes de este arreglo tienen los duplicados y se reprocesan desde `raw/` sin volver a pedir a la fuente. Las tablas `*_history` no cambian: se construían de la unión de jugadores de todas las peticiones y la jornada original ya trae a todos los que la aplazada, así que su identidad estaba completa igualmente.
