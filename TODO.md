# TODO

Pendientes detectados el 2026-09-18 al revisar la data real en Supabase contra la lógica de `scripts/notifier.py`. Nada de esto está implementado todavía.

## 1. Chunking del mensaje de Telegram (notifications-engineer)

`notify_new_opportunities` arma **un solo** mensaje con todas las oportunidades `created` de la corrida y lo manda con un único `send_message`. Telegram rechaza mensajes de más de 4096 caracteres.

- Con 11 oportunidades reales (run del 2026-09-17 16:00 UTC) el mensaje ya pesa **2436 caracteres** (~60% del límite).
- El run inicial del pipeline (2026-09-14 06:00 UTC, antes de que existieran las notificaciones) creó **464 oportunidades de una sola vez** — si eso pasara hoy, el mensaje superaría el límite por mucho.
- Si `send_message` falla, `sync.py` solo loguea un WARNING en el log de GitHub Actions (líneas 158-166) — el usuario no se entera de nada. Es justo el escenario que la notificación debería cubrir.

**Fix:** dividir en varios mensajes cuando el texto supere ~4000 caracteres (por cantidad de items o por longitud acumulada), en vez de un solo `send_message`.

## 2. RLS deshabilitado en las 4 tablas (decisión del usuario)

El advisor de Supabase marca como **crítico**: `opportunities_snapshot`, `opportunities_dim`, `opportunity_events`, `ingestion_runs` tienen Row Level Security deshabilitado. Cualquiera con la anon key puede leer o escribir todas las filas.

- No se aplicó ningún fix automático porque activar RLS sin políticas bloquearía el acceso (incluido Looker Studio).
- Pendiente: decidir si esto importa dado el modelo de acceso actual (¿se usa la service role key en todos lados, o hay algo expuesto con la anon key/publishable key?) y, si aplica, definir las políticas antes de activar RLS.
