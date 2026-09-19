# TODO

## 1. RLS deshabilitado en las 7 tablas (decisión del usuario)

El advisor de Supabase marca como **crítico**: `opportunities_snapshot`, `opportunities_dim`, `opportunity_events`, `ingestion_runs`, `convocatorias_snapshot`, `convocatorias_dim`, `convocatoria_events` tienen Row Level Security deshabilitado. Cualquiera con la anon key puede leer o escribir todas las filas.

- No se aplicó ningún fix automático porque activar RLS sin políticas bloquearía el acceso (incluido Looker Studio).
- Pendiente: decidir si esto importa dado el modelo de acceso actual (¿se usa la service role key en todos lados, o hay algo expuesto con la anon key/publishable key?) y, si aplica, definir las políticas antes de activar RLS.
