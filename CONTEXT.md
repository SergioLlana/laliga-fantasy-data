# La Liga Fantasy Data

Plataforma abierta que ayuda a jugadores de fantasy de La Liga (Biwenger primero, otras plataformas después) con proyecciones de puntos, recomendaciones de mercado, optimización de alineación y alertas de cláusulas.

## Language

**Plataforma de fantasy**:
El juego externo donde el usuario compite (Biwenger es la primera soportada).
_Avoid_: proveedor, juego

**Fuente**:
Un origen de datos externo del que ingerimos información (Biwenger, Transfermarkt, SofaScore...).
_Avoid_: scraper, API (esos son mecanismos de acceso, no la fuente)

**Sistema de puntuación**:
La regla con la que Biwenger convierte la actuación de un jugador en puntos; hay cinco: Picas (AS), SofaScore, Media, Estadísticas y Biwenger Social. Se ingieren los cinco; se modelan todos desde el inicio.
_Avoid_: scoring, modo

**Proyección de puntos**:
La predicción de puntos fantasy de un jugador, por jornada o por temporada, bajo un sistema de puntuación concreto.
_Avoid_: forecast, predicción de rendimiento

**Fichaje**:
Un jugador que aparece en la plantilla de una competición de Biwenger sin puntos en ninguna temporada anterior de esa competición: no tiene historial de La Liga del que proyectar. El ascendido de Segunda también lo es —Segunda es una liga de origen como cualquier otra—. La ingesta diaria los detecta y les descarga identidad e historial bajo demanda.
_Avoid_: jugador nuevo (ambiguo con el recién ascendido), debutante

**Baseline de fichaje**:
La proyección inicial de un jugador recién llegado a La Liga, estimada a partir de su historial en otras ligas.
_Avoid_: cold start (jerga)

**Alerta de cláusula**:
Aviso a un usuario de que la cláusula de un jugador de su liga va a quedar liberada pronto. Requiere datos privados de liga (fase 2).

**Jugador canónico**:
La identidad única de un jugador en nuestra plataforma, con ID propio, a la que apuntan los identificadores de cada fuente.
_Avoid_: golden record, master player

**Mapping**:
La correspondencia aprobada entre el identificador de un jugador o equipo en una fuente y su identidad canónica.
_Avoid_: crosswalk, alias

**Modelo de minutos**:
El modelo que predice cuántos minutos jugará un jugador en una jornada (titularidad, rotación, lesión).

**Modelo de rendimiento**:
El modelo que predice los puntos por partido de un jugador condicionado a que juegue.
_Avoid_: modelo de puntos (ambiguo con la proyección final)

**Nivel de equipo**:
La fuerza de un equipo como variable de los modelos, medida por el valor de plantilla de Transfermarkt. Se aplica al equipo propio y al rival.
_Avoid_: calidad del rival (era la medida anterior, basada en puntos concedidos)

**Nivel de liga**:
La fuerza de una liga de origen para el baseline de fichajes: coeficiente estimado con traslados históricos hacia La Liga, complementado con el valor de plantilla promedio de los equipos de esa liga.
_Avoid_: tier (reservado para la agrupación de ligas en la exploración jerárquica)

**Versión de modelo**:
Un entrenamiento concreto con sus features, preprocesado y datos fijados; está **activa** (alimenta la web), es **candidata** (en evaluación) o está **archivada**.

**Proyección en la sombra**:
La proyección que una versión candidata genera cada jornada sin publicarse en la web, para medir su acierto real antes de promocionarla.
_Avoid_: shadow deployment (jerga)

## Relationships

- Una **Proyección de puntos** se calcula bajo exactamente un **Sistema de puntuación**
- Una **Proyección de puntos** por jornada es el producto del **Modelo de minutos** y el **Modelo de rendimiento**
- Un **Baseline de fichaje** es una **Proyección de puntos** para un jugador sin historial en La Liga
- Cada **Fuente** aporta datos con sus propios identificadores de jugador y equipo
- Un **Jugador canónico** tiene como máximo un **Mapping** por **Fuente**
- Toda **Proyección de puntos** la produce exactamente una **Versión de modelo**; solo la activa se muestra al usuario

## Example dialogue

> **Dev:** "SofaScore le da un 7.2 a Vinícius, ¿eso son sus puntos?"
> **Experto:** "No — esa nota es el insumo del **Sistema de puntuación** SofaScore; Biwenger la convierte en puntos fantasy. Y la fila de SofaScore no vale nada hasta que su ID tenga **Mapping** al **Jugador canónico**."
> **Dev:** "¿Y si el jugador viene del Ajax y no tiene historial en La Liga?"
> **Experto:** "Su proyección inicial es un **Baseline de fichaje**: se estima con sus métricas de eventing y su valor de Transfermarkt en la liga anterior."

## Flagged ambiguities

- "Puntos de Biwenger" es ambiguo: Biwenger no tiene puntos propios, muestra los del **Sistema de puntuación** configurado en cada liga.

## Decisiones abiertas (pendientes de esta sesión)

- Verificar que la API de Biwenger expone los tres sistemas de puntuación por jugador-partido.
- Elegida SofaScore como fuente de eventing primaria; FotMob como candidata secundaria (verificar viabilidad — FBref descartado por bloqueo de scraping).
