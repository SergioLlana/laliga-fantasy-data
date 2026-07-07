# Paso 6 — Aplicación web

**Objetivo:** las cuatro vistas de fase 1 servidas desde las tablas curadas, con el diseño definido en PRODUCT.md (registro: product; analítica y sobria; WCAG AA).

## Decisiones ya tomadas que aplican aquí

- Estructura como `world-cup-predictor/webapp`: FastAPI (`webapp/server.py`) + estáticos (`webapp/static/`), sin framework de frontend pesado.
- Sin base de datos propia: los endpoints leen `curated/` de S3 con DuckDB y caché local con tiempo de vida corto (los datos cambian una vez al día).
- Hosting: Lambda (adaptador Mangum) + estáticos en S3, todo detrás de CloudFront (paso 7).
- El diseño se hace con `/impeccable craft` vista a vista; al empezar este paso se genera `DESIGN.md` (pendiente registrado en `todo.md`).

## Vistas de fase 1

1. **Proyecciones de la jornada** (portada): tabla filtrable por posición, equipo, precio y sistema de puntuación; columnas de minutos esperados, puntos esperados y confianza. Es la vista principal; su densidad y velocidad de escaneo mandan (principio 1 de PRODUCT.md).
2. **Ficha de jugador**: histórico de puntos por partido (5 sistemas), precio Biwenger y valor Transfermarkt en el tiempo, proyección con su explicación (forma, rival, minutos esperados — principio 4).
3. **Mercado**: mayores subidas y bajadas de precio, y ranking de infravalorados (proyección de puntos por millón de precio). Subidas/bajadas nunca solo por color (accesibilidad: signo + flecha).
4. **Fichajes nuevos**: recién llegados con su baseline, la liga de origen, y comparación con 2-3 jugadores conocidos de perfil similar; etiqueta visible de confianza baja cuando aplique (caso 4 del paso 5).

## API

Endpoints JSON de solo lectura, con la forma de las vistas (no de las tablas):

```
GET /api/projections?round=J&score=2&position=DF
GET /api/players/{canonical_id}
GET /api/market/movers?days=7
GET /api/market/value-picks?score=2
GET /api/signings?season=2026
```

Versionados junto a las tablas que leen; si una tabla curada cambia de esquema, el contrato de la API se mantiene (la transformación vive en el servidor).

## Orden de trabajo

1. Servidor FastAPI + lectura DuckDB de `curated/` + endpoints con tests contra datos reales.
2. `/impeccable craft` de la vista de proyecciones (genera también `DESIGN.md` y la configuración del modo live — cerrar los pendientes de `todo.md`).
3. Resto de vistas, una a una, cada una craft + revisión.
4. `/impeccable audit` (accesibilidad AA, responsive) y `/impeccable polish` antes de considerar el paso cerrado.

## Hecho cuando

- Las 4 vistas funcionan en local contra el S3 real.
- Auditoría de accesibilidad AA sin fallos en las 4 vistas.
- La portada carga en <1 s con caché caliente y <3 s en frío.
