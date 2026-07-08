# SofaScore como fuente primaria de eventing

Necesitamos métricas por jugador-partido de muchas ligas para dar un baseline a los fichajes que llegan a La Liga. Elegimos SofaScore como fuente primaria porque su nota de jugador es el insumo directo del sistema de puntuación por defecto de Biwenger (predecir la nota es casi predecir los puntos), cubre prácticamente todas las ligas del mundo y expone una API JSON no oficial.

## Considered Options

- **FBref**: la opción "estándar" (datos Opta muy ricos), descartada porque su bloqueo de scraping se ha endurecido hasta hacerla inviable.
- **Understat**: fácil de scrapear pero solo cubre las 5 grandes ligas — insuficiente para fichajes de Portugal, Holanda, Argentina, etc.
- **FotMob**: fuente secundaria de redundancia, verificada el 2026-07-07 (ver docs/experiments/2026-07-07-alex-fores.md): accesible con impersonación de Chrome, cobertura de ligas menor que SofaScore y nota propia no intercambiable con la de SofaScore.

## Consequences

SofaScore bloquea scrapers: el ingestor necesita throttling educado, caché agresiva de respuestas crudas y una capa de transporte HTTP que permita enchufar un proveedor de proxies (ScrapeOps) si empiezan los bloqueos. Si SofaScore se vuelve inaccesible, el esquema curado debe permitir rellenar las mismas tablas desde otra fuente.
