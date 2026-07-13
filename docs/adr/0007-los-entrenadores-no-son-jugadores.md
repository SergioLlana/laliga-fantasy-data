# Los entrenadores de Biwenger no entran en `biwenger_players`

Biwenger publica a los entrenadores **en la misma lista que a los jugadores**, con ficha completa: `position` 5, precio y puntos. Flick vale 4,42 M y lleva 94 puntos; Mourinho, 4,61 M. En La Liga son 17 fichas de 635. Decidimos que la ingesta los deje fuera de `biwenger_players` (y, con ello, de `fantasy_points` y `biwenger_prices`).

No son jugadores: no tienen minutos, ni eventing en SofaScore, ni existen en la plantilla de Transfermarkt. Como consecuencia, **nunca podrán tener contraparte**, así que caían para siempre en `sin-candidato` y ensuciaban el fichero de revisión con ruido irresoluble.

Peor que el ruido era el daño colateral. El matcher no auto-aprueba a dos entidades de Biwenger que se disputen el mismo candidato de Transfermarkt (issue #40, para que el orden de los ids no decida por nosotros). El entrenador **Simeone** competía con su hijo **Giuliano Simeone** por la misma ficha de Transfermarkt: ninguno de los dos se aprobaba, y Giuliano —que sí es jugador— se quedaba sin mapear por culpa de su padre.

## Considered Options

- **Conservarlos como canónicos solo-Biwenger** (con ID canónico, pero excluidos del matching contra Transfermarkt): mantiene la puerta abierta a proyectarles puntos, pero deja en `biwenger_players` filas que no son jugadores y obliga a que todo consumidor de la tabla —modelo de minutos, modelo de rendimiento, cruces con SofaScore— recuerde filtrarlas. La tabla se llama `players` y debe poder leerse sin notas al pie.
- **Tabla propia `biwenger_coaches`**, con su espacio de IDs canónicos: es lo más explícito y deja abierta la proyección, pero es una tabla, una ingesta y un mapping más para un producto que hoy no existe. Es la opción a la que volver si algún día se quiere el mercado de entrenadores.

## Consequences

Renunciamos, por ahora, a proyectar puntos de entrenadores: la plataforma no podrá recomendarlos ni avisar de su cláusula, aunque en Biwenger sean fichables. Es una decisión de producto, y `PRODUCT.md` no los contempla.

El coste de revertirla es bajo y está acotado: los entrenadores siguen llegando en el `raw/` de la plantilla (la capa curada se reconstruye siempre desde `raw/`, [ADR 0005](0005-transfermarkt-players-particionada-por-temporada.md)), así que recuperarlos es reingerir, no volver a pedirle nada a la fuente.

Al aplicar el filtro, las filas de entrenadores ya ingeridas en `fantasy_points` (775) y `biwenger_prices` (3.102) quedaron huérfanas —puntos de alguien que ya no está en la tabla de jugadores— y se purgaron. Cualquier ingesta anterior a este cambio puede volver a dejarlas: la comprobación es buscar `player_id` sin fila en `biwenger_players`.
