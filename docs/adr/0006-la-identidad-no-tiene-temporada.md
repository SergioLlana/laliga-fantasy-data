# La identidad no tiene temporada: el club es una pista, no un filtro

`transfermarkt_players` está particionada por temporada porque la pertenencia a una plantilla lo está ([ADR 0005](0005-transfermarkt-players-particionada-por-temporada.md)). El mapping heredó esa partición como si fuera suya: buscaba la contraparte de un jugador de Biwenger **solo dentro de la plantilla de la temporada pedida**. Pero un `mapping` no tiene temporada —`players.csv` es `(canonical_id, fuente, id_en_fuente)`, sin año—: dice quién *es* alguien, no dónde jugaba. El club se había colado de filtro cuando su papel era el de pista.

El precio de la confusión: quien no estuviera en la plantilla de la temporada pedida no tenía contraparte posible. Y Biwenger **conserva la ficha del que ya dejó la liga** (con sus puntos, que son historial de entrenamiento), así que 106 de sus 635 jugadores no tenían equipo y caían todos en `sin-candidato` —no porque no existieran en Transfermarkt, sino porque los buscábamos donde no estaban—. Al abrir el pool a las temporadas ya descargadas (1.820 jugadores en vez de 515), 85 de esos 106 encontraron contraparte.

Decidimos que **el club acota, no excluye**, y que la **fecha de nacimiento gradúe la confianza** según de dónde salga el candidato:

| De dónde sale el candidato | Qué se exige para aprobarlo en `auto` |
|---|---|
| Del club ya mapeado | Un único homónimo. La fecha solo **descarta**: si discrepa, va a revisión. |
| Del club, sin homónimo, por fecha | Un único jugador del club nacido ese mismo día. |
| Del pool global (sin club, o sin nadie compatible en él) | La fecha tiene que **confirmar**. Sin fecha que verificar, a revisión. |

La asimetría es deliberada y es el corazón de la decisión: **la fuerza que se le exige a la evidencia depende del tamaño del pool en el que se busca.** Dentro de un club hay ~25 jugadores y un homónimo único ya identifica a alguien; en el pool global hay miles y un apellido suelto no identifica a nadie.

De ahí se sigue una segunda regla, la de **reserva**: quien tiene su identidad confirmada por la fecha se lleva a su candidato, y los demás lo pierden de su lista. El matcher no auto-aprueba a dos entidades de Biwenger que se disputen el mismo candidato (issue #40, para que el orden de los ids no decida), pero una disputa que la evidencia ya resolvió no es una disputa. Sin la reserva, una ficha huérfana de nombre genérico —Biwenger conserva un `Thomas` y un `Adrián` sin equipo— reclama a todos sus homónimos y bloquea a quien está identificado sin ninguna duda: `Adrián` disputaba a 18 Adrianes de Transfermarkt y por sí solo dejaba sin mapear a cinco jugadores reales; `Thomas` bloqueaba a Lemar, que había nacido el día exacto de Thomas Lemar. Un candidato confirmado por *dos* no lo reserva nadie: esa disputa sí es real y va a revisión.

El nivel intermedio (fecha dentro del club) nació de un fallo real: el matcher exige subconjunto de tokens del nombre, y eso nunca casará un apodo con el nombre de pila. `Vinícius Jr` no es subconjunto de `Vinicius Junior` porque `jr` ≠ `junior`; tampoco `Ez Abde` de `Abde Ezzalzouli`, ni `Yusi Enríquez` de `Youssef Enríquez`. Eran 11 jugadores, Vinícius entre ellos, y **los 11 tenían un único compañero de club nacido su mismo día**. Dentro de un club ya mapeado, la fecha identifica sola.

## Considered Options

- **Seguir filtrando por temporada y marcar `skip` a los 106 sin equipo** (darles ID canónico solo con Biwenger): es la salida barata, pero un `skip` se escribe como mapping *aprobado* y el proceso no vuelve a proponer lo aprobado. Cerraría la puerta por dentro: si el jugador vuelve a la liga o ampliamos el histórico, nadie le buscará contraparte. Y tirábamos identidad que existe —65 de ellos tenían contraparte con la fecha coincidente al día—, con ella su valor de mercado y su historial de lesiones y traspasos, justo las features del *nivel de equipo* y del *baseline de fichaje*.
- **Aflojar el matcher de nombres** (fuzzy, distancia de edición, umbral): resolvería los apodos, pero reintroduce lo que [ADR 0001](0001-canonical-player-ids-with-manual-review.md) rechazó —errores de identidad silenciosos—. La fecha de nacimiento es evidencia dura y no necesita umbral.
- **Auto-aprobar en el pool global con nombre único, sin exigir fecha**: cubría 83 de los 106 en vez de 65. Pero de esos 83, cinco tenían la fecha discrepante, y uno era un falso positivo de verdad: el `Luismi` de Biwenger (1992) no es `Luismi Quirant` (2004). Sin la fecha confirmando, ese error habría entrado en silencio.

## Consequences

`--season` deja de decidir a quién se puede mapear y pasa a ser solo lo que dice ser: la temporada de cuyas plantillas salen los clubes. El pool global crece con cada temporada que se descargue, así que la cobertura del mapping mejora sola al ampliar el histórico de Transfermarkt, sin tocar el matcher.

Regenerando los mappings desde cero contra los datos de S3, la cobertura pasó de **500 de 635 jugadores (79%)** a **594 de 618 (96%)**, y la cola de revisión de 135 a 24. Lo que más vale no es la cobertura sino su garantía: **los 594 pares aprobados tienen la fecha de nacimiento coincidente al día, sin una sola discrepancia ni un solo par sin verificar.** Todos los jugadores con equipo en la plantilla actual están mapeados; los 24 que quedan son fichas sin equipo, y cinco de ellas discrepan en la fecha.

Lo que quede en revisión es ahora revisión de verdad —ambigüedad real— y no un artefacto del bloque de búsqueda. A cambio, el matching es más caro (el pool global se recorre por jugador) y aparecen motivos nuevos que el revisor debe entender; están documentados en [`mappings/README.md`](../../mappings/README.md).

Dos discrepancias de fecha detectadas huelen a error de dato en origen más que a personas distintas —`Wesley` (2005-05-03 en Biwenger, 2005-03-05 en Transfermarkt: día y mes intercambiados) y `Cristian Herrera` (un dígito de diferencia)—. La red de la fecha los manda a revisión, que es lo correcto, pero conviene mirar si hay una inversión día/mes en algún parser.
