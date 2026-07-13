# Pendientes

## El id de Biwenger no es estable entre competiciones

Un jugador tiene **un id distinto en cada competición** de Biwenger. Boyomo es
`33694` en La Liga, `22810` (slug `flavien-enzo-boyomo`) en Segunda División y
`35488` en la Copa del Rey. Se ve en el campo `seasons` del detalle por jugador,
que trae un `player: {id, slug}` propio por cada competición en la que jugó.

Esto **romperá la integridad de los mappings en cuanto entre Segunda**: `players.csv`
exige que cada `canonical_id` tenga como máximo una fila por fuente (ADR 0001), y
el mismo jugador tendría dos ids de `biwenger`. Hoy no molesta porque solo se
mapea `la-liga`, donde el id es único.

- [ ] Decidir cómo se modela: ¿la fuente pasa a ser `biwenger:la-liga` /
      `biwenger:segunda`? ¿O un `canonical_id` admite varias filas por fuente
      cuando difieren en competición? Toca `store.py` (la regla de integridad),
      el formato de los CSV y el ADR 0001.
- [ ] Ojo al ascendido: es el caso que fuerza la decisión (un jugador de Segunda
      que llega a La Liga tiene ficha e historial en las dos, y es justo de quien
      más necesitamos el historial para el *baseline de fichaje*).

## Cuando exista el código de la webapp (paso 6 del plan)

- [ ] Generar `DESIGN.md` con `/impeccable document` (o dejar que `/impeccable craft` lo produzca al construir la primera vista). Se decidió a propósito no sembrarlo antes de tener código.
- [ ] Configurar el modo live de impeccable (`.impeccable/live/config.json`): se autoconfigura la primera vez que se ejecute `/impeccable live` con la webapp servida.
- [ ] Construir las vistas de fase 1 con `/impeccable craft`, siguiendo `PRODUCT.md` (registro: product; analítica y sobria; WCAG AA).
