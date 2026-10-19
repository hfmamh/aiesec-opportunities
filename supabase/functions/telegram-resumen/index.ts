// Supabase Edge Function: telegram-resumen
//
// Webhook target for Telegram's getUpdates push (setWebhook), handling:
//   - `/usar_aiesec`, `/usar_convocatorias`, `/usar_getonboard`: sets this
//     chat's *active source* (persisted in telegram_bot_sessions), sticky
//     until changed again.
//   - `/resumen`: a fixed, hardcoded-query summary of the chat's active
//     source (see buildPrompt below). Replies asking to pick a source first
//     if none is set yet.
//   - any other non-slash text: a free-form natural-language question about
//     the chat's active source, answered via Gemini function calling
//     against a small fixed set of safe, parameterized Supabase queries
//     (never raw/model-written SQL — see TOOLS/runTool below).
// All three sources (AIESEC opportunities, convocatorias del Estado
// peruano, Get on Board) are supported identically through the SourceConfig
// below — adding a fourth source means adding one config entry, not new
// handler code. Everything else (the 4h cron sync in scripts/sync.py, its
// own Telegram notifications via scripts/notifier.py, which push to three
// separate per-source broadcast channels) is untouched; this is a separate,
// always-on entry point that lives only here under supabase/functions/.
//
// Required secrets (set via `supabase secrets set` / the dashboard, never
// committed):
//   - TELEGRAM_WEBHOOK_SECRET  — must match the secret_token configured on
//     Telegram's setWebhook call. Requests missing/mismatching this header
//     are rejected with 401 before anything else runs (keeps randoms on the
//     internet from burning the Gemini free-tier quota).
//   - TELEGRAM_BOT_TOKEN       — same bot scripts/notifier.py already uses;
//     not a new bot, just reused here to call sendMessage.
//   - GEMINI_API_KEY           — Gemini REST API key (generativelanguage
//     .googleapis.com), free tier.
// SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are injected automatically into
// every Edge Function's environment — no secret to set for those.
//
// Requires the telegram_bot_sessions table (sql/migrations/0006_add_telegram_bot_sessions.sql).

import { createClient } from "jsr:@supabase/supabase-js@2";

const TELEGRAM_API_BASE = "https://api.telegram.org";
const GEMINI_MODEL = "gemini-flash-lite-latest";
const GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models";

// Safety cap on how many "currently active" rows go into the Gemini
// digest prompt. Keyword-scoped sources (convocatorias) keep this small in
// practice, but this guards against a broad keyword set or an unfiltered
// source ever blowing up the prompt size / token cost.
const MAX_ACTIVE_ROWS = 40;

// Same idea, for the free-question tool-calling flow: a hard server-side
// cap on rows any tool call can return, regardless of what Gemini asks
// for in its function-call args (args.limite below is clamped to this).
const MAX_QUERY_ROWS = 30;

// Cap on tool-calling rounds per free-form question (each round = one
// Gemini call), plus one final forced-text call — worst case 4 Gemini
// calls per question, bounded so a confused model can't loop forever.
const MAX_TOOL_ROUNDS = 3;

type SourceKey = "aiesec" | "convocatorias" | "getonboard";

// Generic row shape — each source's dim table has different columns,
// accessed dynamically through the field names in SourceConfig below.
type SourceRow = Record<string, unknown>;

interface SourceConfig {
  key: SourceKey;
  label: string; // Spanish, used in prompts and user-facing replies
  dimTable: string;
  eventsTable: string;
  eventIdColumn: string; // e.g. "convocatoria_id" in convocatoria_events
  selectFields: string; // computed below from the fields listed here
  titleField: string;
  orgField: string;
  orgLabel: string; // Spanish noun for orgField, e.g. "entidad" / "empresa"
  placeField: string | null;
  placeLabel: string; // e.g. "departamento" / "país" / "ubicación"
  salaryField: string | null;
  salaryIsNumeric: boolean;
  salaryCurrencyField: string | null; // e.g. AIESEC's current_salary_currency
  salaryStaticPrefix: string | null; // e.g. convocatorias' flat "S/"
  dateField: string | null;
  dateLabel: string; // verb phrase, e.g. "cierra" / "publicada"
  dateAbsentLabel: string; // e.g. "sin fecha de cierre"
  highlightTitles?: string[]; // exact-title highlight, e.g. convocatorias' Biólogo/Bióloga
  urlField: string | null; // dim column already holding a full URL, if any
  buildUrl?: (id: string) => string; // for sources with no url column (AIESEC)
}

function buildSelectFields(cfg: Omit<SourceConfig, "selectFields">): string {
  const cols = ["id", cfg.titleField, cfg.orgField];
  if (cfg.placeField) cols.push(cfg.placeField);
  if (cfg.salaryField) cols.push(cfg.salaryField);
  if (cfg.salaryCurrencyField) cols.push(cfg.salaryCurrencyField);
  if (cfg.dateField) cols.push(cfg.dateField);
  if (cfg.urlField) cols.push(cfg.urlField);
  return cols.join(", ");
}

function resolveUrl(cfg: SourceConfig, row: SourceRow): string | null {
  if (cfg.urlField) {
    return (row[cfg.urlField] as string | null) ?? null;
  }
  if (cfg.buildUrl) {
    const id = row.id as string | undefined;
    return id ? cfg.buildUrl(id) : null;
  }
  return null;
}

function defineSource(cfg: Omit<SourceConfig, "selectFields">): SourceConfig {
  return { ...cfg, selectFields: buildSelectFields(cfg) };
}

const SOURCES: Record<SourceKey, SourceConfig> = {
  convocatorias: defineSource({
    key: "convocatorias",
    label: "convocatorias del Estado peruano",
    dimTable: "convocatorias_dim",
    eventsTable: "convocatoria_events",
    eventIdColumn: "convocatoria_id",
    titleField: "current_titulo",
    orgField: "current_entidad",
    orgLabel: "entidad",
    placeField: "current_departamento",
    placeLabel: "departamento",
    salaryField: "current_sueldo",
    salaryIsNumeric: true,
    salaryCurrencyField: null,
    salaryStaticPrefix: "S/",
    dateField: "current_fecha_cierre",
    dateLabel: "cierra",
    dateAbsentLabel: "sin fecha de cierre",
    highlightTitles: ["Biólogo", "Bióloga"],
    urlField: "current_url",
  }),
  aiesec: defineSource({
    key: "aiesec",
    label: "oportunidades AIESEC",
    dimTable: "opportunities_dim",
    eventsTable: "opportunity_events",
    eventIdColumn: "opportunity_id",
    titleField: "current_title",
    orgField: "current_company",
    orgLabel: "empresa",
    placeField: "current_country",
    placeLabel: "país",
    salaryField: "current_salary",
    salaryIsNumeric: true,
    salaryCurrencyField: "current_salary_currency",
    salaryStaticPrefix: null,
    dateField: null,
    dateLabel: "",
    dateAbsentLabel: "",
    urlField: null,
    buildUrl: (id) => `https://aiesec.org/opportunity/global-talent/${id}`,
  }),
  getonboard: defineSource({
    key: "getonboard",
    label: "vacantes de Data Engineer en Get on Board",
    dimTable: "getonboard_dim",
    eventsTable: "getonboard_events",
    eventIdColumn: "getonboard_id",
    titleField: "current_titulo",
    orgField: "current_empresa",
    orgLabel: "empresa",
    placeField: "current_ubicacion",
    placeLabel: "ubicación",
    salaryField: "current_salario",
    salaryIsNumeric: false, // free text (e.g. "S/ 4,000 - 6,000"), not sortable
    salaryCurrencyField: null,
    salaryStaticPrefix: null,
    dateField: "current_fecha_publicacion",
    dateLabel: "publicada",
    dateAbsentLabel: "sin fecha de publicación",
    urlField: "current_url",
  }),
};

const SOURCE_KEYS = Object.keys(SOURCES) as SourceKey[];

function isSourceKey(value: string): value is SourceKey {
  return (SOURCE_KEYS as string[]).includes(value);
}

function supabaseAdminClient() {
  const url = Deno.env.get("SUPABASE_URL");
  const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!url || !key) {
    throw new Error("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set");
  }
  return createClient(url, key);
}

// ============================================================
// Per-chat active source (telegram_bot_sessions)
// ============================================================

async function getActiveSource(
  supabase: ReturnType<typeof createClient>,
  chatId: number | string,
): Promise<SourceKey | null> {
  const { data, error } = await supabase
    .from("telegram_bot_sessions")
    .select("active_source")
    .eq("chat_id", String(chatId))
    .maybeSingle();
  if (error) {
    throw new Error(`telegram_bot_sessions lookup failed: ${error.message}`);
  }
  const value = data?.active_source as string | undefined;
  return value && isSourceKey(value) ? value : null;
}

async function setActiveSource(
  supabase: ReturnType<typeof createClient>,
  chatId: number | string,
  source: SourceKey,
): Promise<void> {
  const { error } = await supabase
    .from("telegram_bot_sessions")
    .upsert({ chat_id: String(chatId), active_source: source, updated_at: new Date().toISOString() });
  if (error) {
    throw new Error(`telegram_bot_sessions upsert failed: ${error.message}`);
  }
}

// ============================================================
// Generic per-source queries
// ============================================================

async function fetchRecientes(
  supabase: ReturnType<typeof createClient>,
  cfg: SourceConfig,
  dias: number,
): Promise<SourceRow[]> {
  const since = new Date(Date.now() - dias * 24 * 60 * 60 * 1000).toISOString();

  const { data: events, error: eventsError } = await supabase
    .from(cfg.eventsTable)
    .select(cfg.eventIdColumn)
    .eq("event_type", "created")
    .gte("event_at", since);
  if (eventsError) {
    throw new Error(`${cfg.eventsTable} query failed: ${eventsError.message}`);
  }

  const ids = [...new Set((events ?? []).map((row) => (row as SourceRow)[cfg.eventIdColumn] as string))];
  if (ids.length === 0) {
    return [];
  }

  const { data: rows, error: dimError } = await supabase
    .from(cfg.dimTable)
    .select(cfg.selectFields)
    .in("id", ids)
    .limit(MAX_QUERY_ROWS);
  if (dimError) {
    throw new Error(`${cfg.dimTable} query (recientes) failed: ${dimError.message}`);
  }
  return (rows ?? []) as SourceRow[];
}

const fetchNuevasEstaSemana = (supabase: ReturnType<typeof createClient>, cfg: SourceConfig) =>
  fetchRecientes(supabase, cfg, 7);

async function fetchActivas(
  supabase: ReturnType<typeof createClient>,
  cfg: SourceConfig,
): Promise<SourceRow[]> {
  let query = supabase.from(cfg.dimTable).select(cfg.selectFields).eq("is_active", true);
  if (cfg.salaryField) {
    query = query.order(cfg.salaryField, { ascending: false, nullsFirst: false });
  } else if (cfg.dateField) {
    query = query.order(cfg.dateField, { ascending: false, nullsFirst: false });
  }
  query = query.limit(MAX_ACTIVE_ROWS);

  const { data, error } = await query;
  if (error) {
    throw new Error(`${cfg.dimTable} query (activas) failed: ${error.message}`);
  }
  return (data ?? []) as SourceRow[];
}

function formatRowLine(cfg: SourceConfig, row: SourceRow): string {
  const titulo = (row[cfg.titleField] as string) || "Sin título";
  const org = (row[cfg.orgField] as string) || `${cfg.orgLabel} desconocida`;
  let line = `- ${titulo} — ${org}`;

  if (cfg.placeField) {
    const place = row[cfg.placeField] as string | null;
    line += place ? ` — ${place}` : ` — ${cfg.placeLabel} no especificado`;
  }

  if (cfg.salaryField) {
    const salary = row[cfg.salaryField];
    if (salary !== null && salary !== undefined && salary !== "") {
      if (cfg.salaryIsNumeric) {
        const amount = Number(salary).toLocaleString("es-PE");
        const currency = cfg.salaryCurrencyField
          ? ((row[cfg.salaryCurrencyField] as string | null) ?? "")
          : (cfg.salaryStaticPrefix ?? "");
        line += ` — ${currency ? `${currency} ${amount}` : amount}`;
      } else {
        line += ` — ${String(salary)}`;
      }
    } else {
      line += " — sin sueldo listado";
    }
  }

  if (cfg.dateField) {
    const date = row[cfg.dateField];
    line += date ? ` (${cfg.dateLabel} ${date})` : ` (${cfg.dateAbsentLabel})`;
  }

  const url = resolveUrl(cfg, row);
  if (url) {
    line += ` — enlace: ${url}`;
  }

  return line;
}

function buildPrompt(cfg: SourceConfig, nuevas: SourceRow[], activas: SourceRow[]): string {
  const nuevasBlock = nuevas.length > 0 ? nuevas.map((r) => formatRowLine(cfg, r)).join("\n") : "(ninguna)";
  const activasBlock = activas.length > 0 ? activas.map((r) => formatRowLine(cfg, r)).join("\n") : "(ninguna)";

  const placeInstruction = cfg.placeField
    ? `- Agrupa por ${cfg.placeLabel} (como máximo 3 grupos, los más representativos) y da el conteo total por ${cfg.orgLabel} dentro de cada grupo (ej. '3 en Essalud'), no listes cada resultado individualmente.`
    : `- Agrupa por ${cfg.orgLabel} y da el conteo total (ej. '3 en Essalud'), no listes cada resultado individualmente.`;

  const salaryInstruction = cfg.salaryField && cfg.salaryIsNumeric
    ? "- Resalta en <b>negrita</b> específicamente el resultado con el sueldo más alto de cada grupo (si hay datos de sueldo), con el emoji 💰 antes del monto."
    : "";

  const dateInstruction = cfg.dateField
    ? `- Usa 📅 para mencionar la fecha (${cfg.dateLabel}) solo de los resultados destacados.`
    : "";

  const highlightInstruction = cfg.highlightTitles && cfg.highlightTitles.length > 0
    ? `- Además, destaca SIEMPRE en <b>negrita</b> con el emoji 🧬 los resultados cuyo título sea NETAMENTE uno de: ${cfg.highlightTitles.join(", ")} (el cargo exacto, sin calificativos como 'técnico', 'analista', 'asistente', 'especialista' u otros) — esto aplica aparte del resaltado por sueldo, incluso si no es el de mayor sueldo.`
    : "";

  return [
    `Eres un asistente que resume ${cfg.label} para enviarlas por Telegram, con un`,
    "estilo dinámico (negritas con emojis, no texto plano). Te doy dos listas ya",
    "filtradas, cada fila con los datos que tiene disponibles, incluyendo su",
    "enlace ('— enlace: URL') cuando existe.",
    "",
    "FORMATO DE SALIDA (obligatorio):",
    "- Usa SOLO HTML válido de Telegram: <b>negrita</b> e <i>cursiva</i>.",
    "  Nunca uses markdown de asteriscos (**texto**) ni encabezados #.",
    placeInstruction,
    salaryInstruction,
    dateInstruction,
    highlightInstruction,
    "- Todo resultado que menciones explícitamente (destacado por sueldo, por",
    "  título, o cualquier otro) debe llevar su enlace como HTML:",
    "  <a href=\"URL exacta de 'enlace'\">Ver</a> pegado al nombre del resultado.",
    "  Nunca muestres la URL como texto plano ni la inventes si no viene en los datos.",
    "- Sé EJECUTIVO y MUY CORTO: máximo ~90 palabras visibles en total (el texto",
    "  de los enlaces, ej. 'Ver', no cuenta para el límite). Nada de saludos,",
    "  despedidas, ni relleno — solo los datos clave, en el menor número de palabras posible.",
    "",
    `Nuevas esta semana (${nuevas.length}):`,
    nuevasBlock,
    "",
    `Activas actualmente (${activas.length}):`,
    activasBlock,
  ].filter((line) => line !== "").join("\n");
}

// Generic Gemini generateContent call, shared by the /resumen prompt-only
// flow and the free-question tool-calling flow below. Returns the raw
// response JSON (not just extracted text) since the tool-calling flow
// needs to inspect candidates for a functionCall part.
async function geminiGenerateContent(
  contents: unknown[],
  tools?: unknown[],
  toolConfig?: unknown,
): Promise<any> {
  const apiKey = Deno.env.get("GEMINI_API_KEY");
  if (!apiKey) {
    throw new Error("GEMINI_API_KEY is not set");
  }

  const body: Record<string, unknown> = { contents };
  if (tools) {
    body.tools = tools;
  }
  if (toolConfig) {
    body.toolConfig = toolConfig;
  }

  const resp = await fetch(`${GEMINI_API_BASE}/${GEMINI_MODEL}:generateContent?key=${apiKey}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!resp.ok) {
    const errBody = await resp.text();
    throw new Error(`Gemini API HTTP ${resp.status}: ${errBody.slice(0, 500)}`);
  }
  return await resp.json();
}

async function callGemini(prompt: string): Promise<string> {
  const json = await geminiGenerateContent([{ role: "user", parts: [{ text: prompt }] }]);
  const text = json?.candidates?.[0]?.content?.parts?.[0]?.text;
  if (typeof text !== "string" || text.trim() === "") {
    throw new Error(`Gemini API returned no usable text: ${JSON.stringify(json).slice(0, 500)}`);
  }
  return text.trim();
}

// ============================================================
// Free-question tool calling (natural-language Q&A)
// ============================================================
//
// Any incoming message that isn't exactly "/resumen" (and doesn't start
// with "/") is treated as a free-form question about the chat's active
// source. Gemini calls the tools below via function calling (up to
// MAX_TOOL_ROUNDS rounds) and each call runs as a safe, parameterized
// supabase-js query — never raw or model-written SQL — before Gemini
// writes the final answer. The tool set is built per-request from the
// active source's config, so the same two tool names ("buscar",
// "recientes") point at a different table depending on which source the
// chat picked with /usar_*.

function buildTools(cfg: SourceConfig): unknown[] {
  const ordenOptions: string[] = [];
  if (cfg.salaryField && cfg.salaryIsNumeric) ordenOptions.push("sueldo_desc (mayor sueldo primero)");
  if (cfg.dateField) {
    ordenOptions.push("fecha_asc (la que vence/cierra antes, primero)");
    ordenOptions.push("fecha_desc (la más reciente primero)");
  }

  const properties: Record<string, unknown> = {
    keyword: {
      type: "STRING",
      description: `Palabra o frase a buscar en el título o en ${cfg.orgLabel}. Omite para no filtrar por palabra clave.`,
    },
    solo_activas: {
      type: "BOOLEAN",
      description: "true (default) para solo resultados activos actualmente; false para incluir también los ya cerrados.",
    },
    limite: {
      type: "INTEGER",
      description: `Máximo de resultados a traer (tope real ${MAX_QUERY_ROWS} sin importar lo que se pida).`,
    },
  };
  if (cfg.placeField) {
    properties.lugar = {
      type: "STRING",
      description: `Filtra por ${cfg.placeLabel}. Omite para no filtrar por lugar.`,
    };
  }
  if (ordenOptions.length > 0) {
    properties.orden = {
      type: "STRING",
      description: `Cómo ordenar los resultados: ${ordenOptions.join(", ")}.`,
    };
  }

  return [
    {
      functionDeclarations: [
        {
          name: "buscar",
          description:
            `Busca ${cfg.label}${cfg.placeField ? ` por ${cfg.placeLabel} y/o` : ", filtrando por"} palabra clave en el título o en ${cfg.orgLabel}, ordenados según corresponda. Úsala para preguntas sobre qué hay disponible, en qué lugar, o cuál es mejor.`,
          parameters: { type: "OBJECT", properties },
        },
        {
          name: "recientes",
          description: `Trae los resultados nuevos (recién publicados) de ${cfg.label} en los últimos N días. Úsala para preguntas sobre qué salió recientemente o esta semana.`,
          parameters: {
            type: "OBJECT",
            properties: {
              dias: { type: "INTEGER", description: "Cuántos días hacia atrás buscar. Default 7." },
            },
          },
        },
      ],
    },
  ];
}

// Strips PostgREST filter-syntax special characters (comma, parens, `*`,
// `%`) from model-supplied filter values before interpolating them into a
// .or()/.imatch() filter string — this is a correctness guard (a stray
// comma/paren breaks the filter's own syntax), not an injection defense:
// supabase-js always sends filter values as separate query parameters, so
// there's no raw SQL string concatenation happening here regardless.
function sanitizeFilterValue(value: unknown, maxLen = 80): string | null {
  if (typeof value !== "string") return null;
  const cleaned = value.replace(/[,()%*]/g, "").trim();
  return cleaned ? cleaned.slice(0, maxLen) : null;
}

// Builds a case-insensitive regex pattern (for PostgREST's `imatch`, ~*)
// that matches `value` regardless of Spanish accents — e.g. "biologo"
// also matches "Biólogo". Plain `ilike` is case-insensitive but NOT
// accent-insensitive in Postgres, so a model-supplied keyword without
// tildes (very common — Gemini itself strips them) was silently matching
// zero rows against accented titles. Escapes regex metacharacters first,
// since this value becomes a POSIX regex, not just an ilike pattern.
const ACCENT_CLASSES: Record<string, string> = {
  a: "[aá]", e: "[eé]", i: "[ií]", o: "[oó]", u: "[uúü]",
  A: "[AÁ]", E: "[EÉ]", I: "[IÍ]", O: "[OÓ]", U: "[UÚÜ]",
  n: "[nñ]", N: "[NÑ]",
};
function accentInsensitivePattern(value: string): string {
  const escaped = value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return escaped.replace(/[aeiouAEIOUnN]/g, (ch) => ACCENT_CLASSES[ch] ?? ch);
}

async function runBuscar(
  supabase: ReturnType<typeof createClient>,
  cfg: SourceConfig,
  args: Record<string, unknown>,
): Promise<SourceRow[]> {
  let query = supabase.from(cfg.dimTable).select(cfg.selectFields);

  const soloActivas = args.solo_activas !== false; // default true
  if (soloActivas) {
    query = query.eq("is_active", true);
  }

  if (cfg.placeField) {
    const lugar = sanitizeFilterValue(args.lugar);
    if (lugar) {
      query = query.imatch(cfg.placeField, accentInsensitivePattern(lugar));
    }
  }

  const keyword = sanitizeFilterValue(args.keyword);
  if (keyword) {
    const pattern = accentInsensitivePattern(keyword);
    query = query.or(`${cfg.titleField}.imatch.${pattern},${cfg.orgField}.imatch.${pattern}`);
  }

  if (args.orden === "sueldo_desc" && cfg.salaryField && cfg.salaryIsNumeric) {
    query = query.order(cfg.salaryField, { ascending: false, nullsFirst: false });
  } else if (args.orden === "fecha_asc" && cfg.dateField) {
    query = query.order(cfg.dateField, { ascending: true, nullsFirst: false });
  } else if (args.orden === "fecha_desc" && cfg.dateField) {
    query = query.order(cfg.dateField, { ascending: false, nullsFirst: false });
  }

  const requestedLimit = typeof args.limite === "number" && args.limite > 0 ? Math.floor(args.limite) : MAX_QUERY_ROWS;
  query = query.limit(Math.min(requestedLimit, MAX_QUERY_ROWS));

  const { data, error } = await query;
  if (error) {
    throw new Error(`buscar (${cfg.key}) query failed: ${error.message}`);
  }
  return (data ?? []) as SourceRow[];
}

async function runRecientes(
  supabase: ReturnType<typeof createClient>,
  cfg: SourceConfig,
  args: Record<string, unknown>,
): Promise<SourceRow[]> {
  const dias = typeof args.dias === "number" && args.dias > 0 ? Math.min(Math.floor(args.dias), 90) : 7;
  return fetchRecientes(supabase, cfg, dias);
}

async function runTool(
  supabase: ReturnType<typeof createClient>,
  cfg: SourceConfig,
  name: string,
  args: Record<string, unknown>,
): Promise<SourceRow[]> {
  if (name === "buscar") return runBuscar(supabase, cfg, args);
  if (name === "recientes") return runRecientes(supabase, cfg, args);
  throw new Error(`Unknown tool requested by Gemini: ${name}`);
}

function qaSystemPreamble(cfg: SourceConfig): string {
  return [
    `Eres un asistente de Telegram sobre ${cfg.label}. El usuario te hace una`,
    "pregunta en lenguaje natural. Si la pregunta necesita datos (buscar por",
    "lugar o palabra clave, ordenar por sueldo o fecha, o ver qué salió",
    "recientemente), llama a la función correspondiente — nunca inventes",
    "datos. Si la pregunta no tiene que ver con esto o no se puede responder",
    "con las funciones disponibles, dilo brevemente y sugiere /resumen, o",
    "cambiar de fuente con /usar_aiesec, /usar_convocatorias o",
    "/usar_getonboard.",
    "",
    "Cuando redactes la respuesta final (después de ver el resultado de la",
    "función, si la llamaste), usa SOLO HTML válido de Telegram",
    "(<b>negrita</b>, <i>cursiva</i>; nunca markdown de asteriscos ni",
    "encabezados #), sé breve y directo, y usa 💰 para sueldos y 📅 para",
    "fechas cuando corresponda. Cada resultado que los datos de la función",
    "incluyan con '— enlace: URL' y que menciones en tu respuesta debe llevar",
    "ese enlace como HTML (<a href=\"URL exacta\">Ver</a> pegado al nombre) —",
    "nunca como texto plano ni inventado.",
  ].join("\n");
}

async function handleFreeQuestion(
  supabase: ReturnType<typeof createClient>,
  cfg: SourceConfig,
  chatId: number | string,
  question: string,
): Promise<void> {
  const tools = buildTools(cfg);

  const contents: Record<string, unknown>[] = [
    { role: "user", parts: [{ text: `${qaSystemPreamble(cfg)}\n\nPregunta del usuario: ${question}` }] },
  ];

  // Bounded tool loop: this model often wants several lookups (e.g. one
  // filtered by a place AND one unfiltered, emitted together, or a second
  // search after seeing the first), so a single round left it without a
  // text answer. Each round executes every functionCall in the turn.
  for (let round = 0; round < MAX_TOOL_ROUNDS; round++) {
    const resp = await geminiGenerateContent(contents, tools);
    const content = resp?.candidates?.[0]?.content;
    const parts: Record<string, unknown>[] = content?.parts ?? [];
    const calls = parts.filter((p) => p.functionCall);

    if (calls.length === 0) {
      const text = extractText(parts);
      if (text) {
        await sendTelegramMessage(chatId, text);
        return;
      }
      break;
    }

    // The model's turn goes back verbatim: Gemini 3.x functionCall parts
    // carry a `thoughtSignature` the API requires on the follow-up.
    contents.push(content);
    const responses = await Promise.all(calls.map(async (p) => {
      const { name, args, id } = p.functionCall as { name: string; args?: Record<string, unknown>; id?: string };
      let response: Record<string, unknown>;
      try {
        const rows = await runTool(supabase, cfg, name, args ?? {});
        response = { count: rows.length, resultados: rows.map((r) => formatRowLine(cfg, r)) };
      } catch (err) {
        console.error("telegram-resumen: tool execution failed", name, err);
        response = { error: err instanceof Error ? err.message : String(err) };
      }
      // `id` correlates each response with its call; role must be "user"
      // (this API version rejects role "function").
      return { functionResponse: { name, response, ...(id ? { id } : {}) } };
    }));
    contents.push({ role: "user", parts: responses });
  }

  // Out of rounds (or an empty turn): force a text answer from whatever
  // data has been gathered so far, so the user never gets a dead end.
  const final = await geminiGenerateContent(contents, tools, { functionCallingConfig: { mode: "NONE" } });
  const finalText = extractText(final?.candidates?.[0]?.content?.parts ?? []);
  if (!finalText) {
    console.error("telegram-resumen: no usable final text", JSON.stringify(final).slice(0, 500));
    await sendTelegramMessage(chatId, "No pude armar una respuesta clara para eso. Prueba /resumen o reformula la pregunta.");
    return;
  }
  await sendTelegramMessage(chatId, finalText);
}

// Joins the visible text parts of a turn, skipping thought-summary parts.
function extractText(parts: Record<string, unknown>[]): string {
  return parts
    .filter((p) => typeof p.text === "string" && !p.thought)
    .map((p) => p.text as string)
    .join("")
    .trim();
}

async function sendTelegramMessage(
  chatId: number | string,
  text: string,
  parseMode: "HTML" | undefined = "HTML",
): Promise<void> {
  const token = Deno.env.get("TELEGRAM_BOT_TOKEN");
  if (!token) {
    throw new Error("TELEGRAM_BOT_TOKEN is not set");
  }

  const payload: Record<string, unknown> = {
    chat_id: chatId,
    text,
    disable_web_page_preview: true,
  };
  if (parseMode) {
    payload.parse_mode = parseMode;
  }

  const resp = await fetch(`${TELEGRAM_API_BASE}/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  const body = await resp.json().catch(() => null);
  if (!resp.ok || !body?.ok) {
    // Gemini-generated HTML can occasionally be malformed (unescaped "<"/"&"
    // from a raw title, an unclosed tag). Rather than lose the summary
    // entirely, retry once as plain text so the user still gets the
    // content — only for this specific Telegram "can't parse entities"
    // failure mode, not for other errors (bad token, chat not found, etc.).
    const description = typeof body?.description === "string" ? body.description : "";
    if (parseMode && resp.status === 400 && /can't parse entities/i.test(description)) {
      console.error("telegram-resumen: HTML parse failed, retrying as plain text", description);
      await sendTelegramMessage(chatId, text, undefined);
      return;
    }
    throw new Error(`Telegram sendMessage failed (HTTP ${resp.status}): ${JSON.stringify(body).slice(0, 500)}`);
  }
}

async function handleResumen(
  supabase: ReturnType<typeof createClient>,
  cfg: SourceConfig,
  chatId: number | string,
): Promise<void> {
  const [nuevas, activas] = await Promise.all([
    fetchNuevasEstaSemana(supabase, cfg),
    fetchActivas(supabase, cfg),
  ]);

  if (nuevas.length === 0 && activas.length === 0) {
    await sendTelegramMessage(
      chatId,
      `No hay resultados nuevos esta semana ni activos en este momento para ${cfg.label}.`,
    );
    return;
  }

  const prompt = buildPrompt(cfg, nuevas, activas);
  const summary = await callGemini(prompt);
  await sendTelegramMessage(chatId, summary);
}

const USAR_COMMAND_PREFIX = "/usar_";

Deno.serve(async (req: Request) => {
  try {
    if (req.method !== "POST") {
      return new Response("ok", { status: 200 });
    }

    const expectedSecret = Deno.env.get("TELEGRAM_WEBHOOK_SECRET");
    const providedSecret = req.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (!expectedSecret || providedSecret !== expectedSecret) {
      console.error("telegram-resumen: rejected request with missing/invalid secret token");
      return new Response("unauthorized", { status: 401 });
    }

    let update: Record<string, unknown>;
    try {
      update = await req.json();
    } catch (err) {
      console.error("telegram-resumen: failed to parse webhook body as JSON", err);
      return new Response("ok", { status: 200 });
    }

    const message = update?.message as Record<string, unknown> | undefined;
    const chat = message?.chat as Record<string, unknown> | undefined;
    const chatId = chat?.id as number | string | undefined;
    const rawText = message?.text;
    const trimmedText = typeof rawText === "string" ? rawText.trim() : "";
    const lowerText = trimmedText.toLowerCase();

    if (trimmedText === "") {
      // No text (e.g. a sticker/photo update) — nothing to do.
      return new Response("ok", { status: 200 });
    }

    if (chatId === undefined || chatId === null) {
      console.error("telegram-resumen: update had no chat id", JSON.stringify(update).slice(0, 500));
      return new Response("ok", { status: 200 });
    }

    const supabase = supabaseAdminClient();

    try {
      if (lowerText.startsWith(USAR_COMMAND_PREFIX)) {
        const candidate = lowerText.slice(USAR_COMMAND_PREFIX.length);
        if (isSourceKey(candidate)) {
          await setActiveSource(supabase, chatId, candidate);
          await sendTelegramMessage(
            chatId,
            `Listo — ahora estás consultando <b>${SOURCES[candidate].label}</b>. Usa /resumen o pregúntame lo que quieras.`,
          );
        } else {
          await sendTelegramMessage(
            chatId,
            "Fuente no reconocida. Usa /usar_aiesec, /usar_convocatorias o /usar_getonboard.",
          );
        }
        return new Response("ok", { status: 200 });
      }

      if (lowerText.startsWith("/") && lowerText !== "/resumen") {
        // Any other bot command (e.g. /start) is ignored, not treated as a
        // free-form question.
        return new Response("ok", { status: 200 });
      }

      const activeKey = await getActiveSource(supabase, chatId);
      if (!activeKey) {
        await sendTelegramMessage(
          chatId,
          "Primero elige una fuente: /usar_aiesec, /usar_convocatorias o /usar_getonboard.",
        );
        return new Response("ok", { status: 200 });
      }
      const cfg = SOURCES[activeKey];

      if (lowerText === "/resumen") {
        await handleResumen(supabase, cfg, chatId);
      } else {
        await handleFreeQuestion(supabase, cfg, chatId, trimmedText);
      }
    } catch (err) {
      console.error("telegram-resumen: failed to handle update", err);
      try {
        await sendTelegramMessage(
          chatId,
          "Ocurrió un error generando la respuesta. Intenta de nuevo en unos minutos.",
        );
      } catch (notifyErr) {
        console.error("telegram-resumen: also failed to notify the user of the error", notifyErr);
      }
    }

    return new Response("ok", { status: 200 });
  } catch (err) {
    console.error("telegram-resumen: unhandled error", err);
    return new Response("ok", { status: 200 });
  }
});
