# TODO

## 1. RLS deshabilitado en todas las tablas (decisión del usuario)

El advisor de Supabase marca como **crítico** que ninguna tabla tiene Row Level Security: `opportunities_snapshot`, `opportunities_dim`, `opportunity_events`, `convocatorias_snapshot`, `convocatorias_dim`, `convocatoria_events`, `convocatoria_topics`, `convocatoria_keywords`, `convocatoria_topic_matches`, `getonboard_snapshot`, `getonboard_dim`, `getonboard_events`, `ingestion_runs`, `notification_channels` y `telegram_bot_sessions`. Cualquiera con la anon key puede leer o escribir todas las filas.

- No se aplicó ningún fix automático porque activar RLS sin políticas bloquearía el acceso (incluido Looker Studio).
- Pendiente: decidir si esto importa dado el modelo de acceso actual (¿se usa la service role key en todos lados, o hay algo expuesto con la anon key/publishable key?) y, si aplica, definir las políticas antes de activar RLS. Es obligatorio antes de publicar cualquier página web que use la anon key.

## 2. El bot de Telegram responde a cualquier chat

`telegram-resumen` solo verifica que el request venga de Telegram (webhook secret), no quién escribe. Cualquiera que encuentre el bot puede usar `/resumen` o hacer preguntas y gastar la cuota gratuita de Gemini.

- Pendiente: una lista de `chat_id` autorizados (tabla o secret) y que el resto de chats se ignore.
