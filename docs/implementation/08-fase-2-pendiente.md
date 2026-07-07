# Paso 8 — Fase 2: cuenta conectada (pendiente de decisiones)

**Objetivo (cuando se aborde):** alertas de cláusulas y optimización de alineación sobre la liga privada del usuario.

Este documento no es un plan de implementación: registra qué está decidido, qué falta por decidir y qué condiciones deben cumplirse antes de planificarlo en detalle. Se decidió a propósito (sesión del 2026-07-07) no resolver esto todavía.

## Lo que ya está decidido

- La fase 2 no empieza hasta que la fase 1 esté desplegada y estable (pasos 1-7 cerrados).
- No se pedirá a los usuarios su contraseña de Biwenger (descartado en la sesión de planificación por riesgo legal y de seguridad).

## Decisiones abiertas (bloquean la planificación)

1. **Cómo obtiene el usuario su token de Biwenger** de forma aceptable: pegarlo desde las herramientas del navegador (torpe pero seguro), una extensión de navegador propia, u otra vía. Requiere investigar también la parte legal (términos de uso de Biwenger).
2. **Dónde se guardan los tokens** y con qué cifrado; qué pasa cuando caducan.
3. **Sistema de usuarios**: registro/login propio de la plataforma (hasta ahora no hay ninguno; la fase 1 es pública sin cuentas).
4. **Canal de las alertas de cláusulas**: correo, Telegram, notificación web. Ninguno decidido.
5. **Semántica exacta de la alerta**: en Biwenger la cláusula de un jugador queda protegida un tiempo tras su compra; la alerta útil es "la protección de X en tu liga expira en N horas y su cláusula es pagable por ti". Hay que verificar qué expone la API de liga privada (endpoints, campos, frecuencia razonable de consulta sin abusar).

## Trabajo previo que la fase 1 ya deja hecho

- Toda la capa de datos públicos, mappings y proyecciones (las alertas y el optimizador consumen `projections` tal cual).
- El optimizador de alineación es un problema de optimización con restricciones (formaciones válidas, 11 jugadores) sobre proyecciones ya existentes: no necesita datos nuevos, solo el equipo del usuario.

## Cuándo abrirlo

Revisar tras cerrar el paso 7, con una sesión de planificación propia (/grill-with-docs) dedicada a las 5 decisiones abiertas.
