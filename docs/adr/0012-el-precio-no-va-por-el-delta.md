# El precio de Biwenger no se ingiere por `--delta`

`--delta` refresca solo a quienes puntuaron en la jornada (eficiente para `fantasy_points`, donde el no-puntuador no genera fila). Pero el **Precio** es una señal de la plantilla entera, independiente de si el jugador jugó, así que estrangularlo con `--delta` deja sin precio a los no-puntuadores durante la temporada. Decidimos **desacoplar el precio del delta**: la serie `biwenger_prices` se mantiene hacia delante con el **snapshot diario de plantilla** (`CompetitionData.Player.price`, una sola petición para todos los jugadores), que es exactamente la misma métrica que la serie diaria del detalle por jugador (verificado empíricamente el 2026-07-16: `price` == valores de `PlayerDetail.prices`; `fantasyPrice` es la cláusula, en otra escala, y `priceIncrement` es el delta día a día). Como el snapshot solo captura el precio del día en que corre, un **barrido periódico del detalle** —que ya se pide para puntos y fecha de nacimiento— actúa de red de seguridad rellenando huecos dentro de la ventana móvil de ~366 días (opción A2). `--delta` queda **solo** para `fantasy_points`.

## Considered Options

- **Precio por `--delta` (statu quo)**: rechazada. Acopla dos cadencias distintas (puntos = por jornada, solo scorers; precio = diario, plantilla entera) y abre un hueco en `biwenger_prices` para los no-puntuadores, contradiciendo el "el histórico de precios se acumula hacia delante" de [#89](https://github.com/SergioLlana/laliga-fantasy-data/issues/89).
- **Barrido completo del detalle, periódico**: capturaría toda la ventana de 366 días de golpe, pero cuesta ~634 peticiones por barrido (varias ventanas de cuota, [ADR 0004](0004-scrapeops-como-desbordamiento.md)) frente a 1 del snapshot. Se conserva solo como red de seguridad, no como mantenimiento principal.

## Consequences

- El pipeline diario ([#24](https://github.com/SergioLlana/laliga-fantasy-data/issues/24)) gana un paso de snapshot de plantilla (1 petición/día).
- La completitud diaria de la serie depende de que ese paso corra a diario; el barrido periódico del detalle la repara si un día no corre, mientras el hueco quede dentro de la ventana de ~366 días.
- No hay histórico de precios anterior a esa ventana: solo se acumula hacia delante (ver [#89](https://github.com/SergioLlana/laliga-fantasy-data/issues/89)).
