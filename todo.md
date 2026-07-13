# Pendientes

## ~~El id de Biwenger no es estable entre competiciones~~ (resuelto 2026-07-13)

Confirmado (y aplica también a equipos: Espanyol es `7` en La Liga y `542` en
Segunda). Resuelto por la vía de no modelarlo: **de Biwenger se ingiere
exclusivamente `la-liga`** y Segunda/Copa se cubren con Transfermarkt/SofaScore
como cualquier liga de origen — [ADR 0008](docs/adr/0008-de-biwenger-solo-se-ingiere-la-liga.md).
Guardarraíl en `COMPETITIONS` del cliente y en las opciones del CLI. El historial
del ascendido para el *baseline de fichaje* sale de SofaScore, con su conteo de
minutos (la validación tipo Forés con puntos reales de Segunda queda como
experimento ad hoc contra raw).

## Cuando exista el código de la webapp (paso 6 del plan)

- [ ] Generar `DESIGN.md` con `/impeccable document` (o dejar que `/impeccable craft` lo produzca al construir la primera vista). Se decidió a propósito no sembrarlo antes de tener código.
- [ ] Configurar el modo live de impeccable (`.impeccable/live/config.json`): se autoconfigura la primera vez que se ejecute `/impeccable live` con la webapp servida.
- [ ] Construir las vistas de fase 1 con `/impeccable craft`, siguiendo `PRODUCT.md` (registro: product; analítica y sobria; WCAG AA).
