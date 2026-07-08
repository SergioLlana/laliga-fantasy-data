# IDs canónicos propios con revisión manual de mappings

Cada fuente (Biwenger, SofaScore, Transfermarkt...) identifica a jugadores y equipos con sus propios IDs y variantes de nombre. Decidimos crear un ID canónico propio por jugador y equipo, con una tabla de mappings por fuente: el matching automático (nombre normalizado + equipo + fecha de nacimiento + posición) resuelve los casos claros, y los dudosos van a un fichero de revisión versionado en git que se aprueba a mano.

## Considered Options

- **Fuzzy matching automático con umbral**: cero trabajo manual, pero los errores de identidad son silenciosos y envenenan el entrenamiento de los modelos.
- **ID de Transfermarkt como pivote**: ahorra una tabla, pero nos ata a una fuente externa que puede fallar o banearnos, y no todos los jugadores de Biwenger existen allí.

## Consequences

El ID canónico se propaga a todas las tablas curadas y a los modelos, por lo que esta decisión es la más cara de revertir del proyecto. El coste manual se concentra en el backfill inicial; después solo hay que revisar fichajes nuevos.
