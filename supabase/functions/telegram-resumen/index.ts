// Supabase Edge Function: telegram-resumen
//
// Webhook target for Telegram's getUpdates push (setWebhook), handling:
//   - `/resumen`: a fixed, hardcoded-query summary (see buildPrompt below).
//   - any other non-slash text: a free-form natural-language question,
//     answered via Gemini function calling against a small fixed set of
//     safe, parameterized Supabase queries (never raw/model-written SQL —
//     see TOOLS/runTool below).
// Both reply in real time with Gemini-generated text about convocatorias
// del Estado peruano — the only source wired up so far (not AIESEC, not Get
// on Board). Everything else (the 4h cron sync in scripts/sync.py, its own
// Telegram notifications via scripts/notifier.py) is untouched; this is a
// separate, always-on entry point that lives only here under
// supabase/functions/.
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

import { createClient } from "jsr:@supabase/supabase-js@2";

const TELEGRAM_API_BASE = "https://api.telegram.org";
const GEMINI_MODEL = "gemini-flash-lite-latest";
const GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models";

// Safety cap on how many "currently active" convocatorias go into the
// Gemini prompt. The keyword-scoped scrape (convocatoria_keywords, see
// sql/schema.sql) keeps this small in practice, but this guards against a
// broad keyword set ever blowing up the prompt size / token cost.
const MAX_ACTIVE_ROWS = 40;

// Same idea, for the free-question tool-calling flow: a hard server-side
// cap on rows any tool call can return, regardless of what Gemini asks
// for in its function-call args (args.limite below is clamped to this).
const MAX_QUERY_ROWS = 30;

// Cap on tool-calling rounds per free-form question (each round = one
// Gemini call), plus one final forced-text call — worst case 4 Gemini
// calls per question, bounded so a confused model can't loop forever.
const MAX_TOOL_ROUNDS = 3;

interface ConvocatoriaRow {
  id: string;
  current_titulo: string | null;
  current_entidad: string | null;
  current_fecha_cierre: string | null;
  current_departamento: string | null;
  current_sueldo: number | null;
}

const SELECT_FIELDS =
  "id, current_titulo, current_entidad, current_fecha_cierre, current_departamento, current_sueldo";

function supabaseAdminClient() {
  const url = Deno.env.get("SUPABASE_URL");
  const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!url || !key) {
    throw new Error("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set");
  }
  return createClient(url, key);
}

async function fetchRecientes(
  supabase: ReturnType<typeof createClient>,
  dias: number,
): Promise<ConvocatoriaRow[]> {
  const since = new Date(Date.now() - dias * 24 * 60 * 60 * 1000).toISOString();

  const { data: events, error: eventsError } = await supabase
    .from("convocatoria_events")
    .select("convocatoria_id")
    .eq("event_type", "created")
    .gte("event_at", since);
  if (eventsError) {
    throw new Error(`convocatoria_events query failed: ${eventsError.message}`);
  }

  const ids = [...new Set((events ?? []).map((row) => row.convocatoria_id as string))];
  if (ids.length === 0) {
    return [];
  }

  const { data: rows, error: dimError } = await supabase
    .from("convocatorias_dim")
    .select(SELECT_FIELDS)
    .in("id", ids)
    .limit(MAX_QUERY_ROWS);
  if (dimError) {
    throw new Error(`convocatorias_dim query (recientes) failed: ${dimError.message}`);
  }
  return (rows ?? []) as ConvocatoriaRow[];
}

const fetchNuevasEstaSemana = (supabase: ReturnType<typeof createClient>) => fetchRecientes(supabase, 7);

async function fetchActivas(
  supabase: ReturnType<typeof createClient>,
): Promise<ConvocatoriaRow[]> {
  const { data: rows, error } = await supabase
    .from("convocatorias_dim")
    .select(SELECT_FIELDS)
    .eq("is_active", true)
    .order("current_sueldo", { ascending: false, nullsFirst: false })
    .limit(MAX_ACTIVE_ROWS);
  if (error) {
    throw new Error(`convocatorias_dim query (activas) failed: ${error.message}`);
  }
  return (rows ?? []) as ConvocatoriaRow[];
}

function formatRowLine(row: ConvocatoriaRow): string {
  const titulo = row.current_titulo || "Convocatoria sin título";
  const entidad = row.current_entidad || "Entidad desconocida";
  const depto = row.current_departamento || "Departamento no especificado";
  const cierre = row.current_fecha_cierre ? `cierra ${row.current_fecha_cierre}` : "sin fecha de cierre";
  const sueldo = row.current_sueldo ? `S/ ${row.current_sueldo.toLocaleString("es-PE")}` : "sin sueldo listado";
  return `- ${titulo} — ${entidad} — depto: ${depto} — ${sueldo} (${cierre})`;
}

function buildPrompt(nuevas: ConvocatoriaRow[], activas: ConvocatoriaRow[]): string {
  const nuevasBlock = nuevas.length > 0
    ? nuevas.map(formatRowLine).join("\n")
    : "(ninguna)";
  const activasBlock = activas.length > 0
    ? activas.map(formatRowLine).join("\n")
    : "(ninguna)";

  return [
    "Eres un asistente que resume convocatorias laborales del Estado peruano",
    "para enviarlas por Telegram, con el mismo estilo dinámico que usan los",
    "mensajes automáticos de este canal (negritas con emojis, no texto",
    "plano). Te doy dos listas ya filtradas por los temas de interés del",
    "usuario, cada fila con su departamento y sueldo (si existe).",
    "",
    "FORMATO DE SALIDA (obligatorio):",
    "- Usa SOLO HTML válido de Telegram: <b>negrita</b> e <i>cursiva</i>.",
    "  Nunca uses markdown de asteriscos (**texto**) ni encabezados #.",
    "- Dos párrafos, cada uno con un emoji de encabezado:",
    "  '🏙️ <b>Lima</b>' para convocatorias cuyo departamento sea Lima",
    "  (incluye Callao/Lima Metropolitana), y",
    "  '🗺️ <b>Provincias</b>' para todo el resto. Si un párrafo no tiene",
    "  nada que mostrar, omítelo por completo.",
    "- Dentro de cada párrafo, agrupa por entidad y da el conteo total",
    "  (ej. '3 en MINSA'), no listes cada convocatoria individualmente.",
    "- Resalta en <b>negrita</b> específicamente la(s) convocatoria(s) con",
    "  el sueldo más alto de cada párrafo, con el emoji 💰 antes del monto",
    "  (ej. '💰 <b>S/ 5,500</b> en Essalud').",
    "- Usa 📅 para mencionar fechas de cierre solo de las destacadas.",
    "- Además, destaca SIEMPRE en <b>negrita</b> con el emoji 🧬 las",
    "  convocatorias cuyo título sea NETAMENTE 'Biólogo' o 'Bióloga' (el",
    "  cargo exacto, sin calificativos como 'técnico', 'analista',",
    "  'asistente', 'especialista' u otros) — esto aplica aparte del",
    "  resaltado por sueldo, incluso si no es la de mayor sueldo.",
    "- Sé EJECUTIVO y MUY CORTO: máximo ~90 palabras en total entre ambos",
    "  párrafos. Nada de saludos, despedidas, ni relleno — solo los datos",
    "  clave, en el menor número de palabras posible.",
    "",
    `Nuevas esta semana (${nuevas.length}):`,
    nuevasBlock,
    "",
    `Activas actualmente (${activas.length}):`,
    activasBlock,
  ].join("\n");
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
// with "/") is treated as a free-form question. Gemini calls the tools
// below via function calling (up to MAX_TOOL_ROUNDS rounds) and each call
// runs as a safe, parameterized supabase-js query — never raw or
// model-written SQL — before Gemini writes the final answer.

const TOOLS = [
  {
    functionDeclarations: [
      {
        name: "buscar_convocatorias",
        description:
          "Busca convocatorias del Estado peruano (ya filtradas por los temas de interés configurados) por departamento y/o palabra clave en el título o la entidad convocante, ordenadas por sueldo o por fecha de cierre. Úsala para preguntas sobre qué hay disponible, en qué lugar, o cuál paga más / cierra más pronto.",
        parameters: {
          type: "OBJECT",
          properties: {
            departamento: {
              type: "STRING",
              description: "Filtra por departamento/región del Perú, ej. 'Lima', 'Arequipa'. Omite para no filtrar por lugar.",
            },
            keyword: {
              type: "STRING",
              description: "Palabra o frase a buscar en el título del puesto o en la entidad convocante. Omite para no filtrar por palabra clave.",
            },
            solo_activas: {
              type: "BOOLEAN",
              description: "true (default) para solo convocatorias activas actualmente; false para incluir también las ya cerradas.",
            },
            orden: {
              type: "STRING",
              enum: ["sueldo_desc", "cierre_proximo"],
              description: "Cómo ordenar los resultados: 'sueldo_desc' = mayor sueldo primero, 'cierre_proximo' = las que cierran más pronto primero.",
            },
            limite: {
              type: "INTEGER",
              description: `Máximo de resultados a traer (tope real ${MAX_QUERY_ROWS} sin importar lo que se pida).`,
            },
          },
        },
      },
      {
        name: "convocatorias_recientes",
        description:
          "Trae las convocatorias nuevas (recién publicadas) en los últimos N días. Úsala para preguntas sobre qué salió recientemente o esta semana.",
        parameters: {
          type: "OBJECT",
          properties: {
            dias: {
              type: "INTEGER",
              description: "Cuántos días hacia atrás buscar nuevas publicaciones. Default 7.",
            },
          },
        },
      },
    ],
  },
];

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

async function runBuscarConvocatorias(
  supabase: ReturnType<typeof createClient>,
  args: Record<string, unknown>,
): Promise<ConvocatoriaRow[]> {
  let query = supabase.from("convocatorias_dim").select(SELECT_FIELDS);

  const soloActivas = args.solo_activas !== false; // default true
  if (soloActivas) {
    query = query.eq("is_active", true);
  }

  const departamento = sanitizeFilterValue(args.departamento);
  if (departamento) {
    query = query.imatch("current_departamento", accentInsensitivePattern(departamento));
  }

  const keyword = sanitizeFilterValue(args.keyword);
  if (keyword) {
    const pattern = accentInsensitivePattern(keyword);
    query = query.or(`current_titulo.imatch.${pattern},current_entidad.imatch.${pattern}`);
  }

  if (args.orden === "sueldo_desc") {
    query = query.order("current_sueldo", { ascending: false, nullsFirst: false });
  } else if (args.orden === "cierre_proximo") {
    query = query.order("current_fecha_cierre", { ascending: true, nullsFirst: false });
  }

  const requestedLimit = typeof args.limite === "number" && args.limite > 0 ? Math.floor(args.limite) : MAX_QUERY_ROWS;
  query = query.limit(Math.min(requestedLimit, MAX_QUERY_ROWS));

  const { data, error } = await query;
  if (error) {
    throw new Error(`buscar_convocatorias query failed: ${error.message}`);
  }
  return (data ?? []) as ConvocatoriaRow[];
}

async function runConvocatoriasRecientes(
  supabase: ReturnType<typeof createClient>,
  args: Record<string, unknown>,
): Promise<ConvocatoriaRow[]> {
  const dias = typeof args.dias === "number" && args.dias > 0 ? Math.min(Math.floor(args.dias), 90) : 7;
  return fetchRecientes(supabase, dias);
}

async function runTool(
  supabase: ReturnType<typeof createClient>,
  name: string,
  args: Record<string, unknown>,
): Promise<ConvocatoriaRow[]> {
  if (name === "buscar_convocatorias") return runBuscarConvocatorias(supabase, args);
  if (name === "convocatorias_recientes") return runConvocatoriasRecientes(supabase, args);
  throw new Error(`Unknown tool requested by Gemini: ${name}`);
}

const QA_SYSTEM_PREAMBLE = [
  "Eres un asistente de Telegram sobre convocatorias laborales del Estado",
  "peruano, ya filtradas por los temas de interés configurados (actualmente",
  "biología). El usuario te hace una pregunta en lenguaje natural. Si la",
  "pregunta necesita datos (buscar por lugar o palabra clave, ordenar por",
  "sueldo o fecha de cierre, o ver qué salió recientemente), llama a la",
  "función correspondiente — nunca inventes datos. Si la pregunta no tiene",
  "que ver con estas convocatorias o no se puede responder con las",
  "funciones disponibles, dilo brevemente y sugiere escribir /resumen para",
  "un resumen general.",
  "",
  "Cuando redactes la respuesta final (después de ver el resultado de la",
  "función, si la llamaste), usa SOLO HTML válido de Telegram",
  "(<b>negrita</b>, <i>cursiva</i>; nunca markdown de asteriscos ni",
  "encabezados #), sé breve y directo, y usa 💰 para sueldos y 📅 para",
  "fechas de cierre cuando corresponda.",
].join("\n");

async function handleFreeQuestion(chatId: number | string, question: string): Promise<void> {
  const supabase = supabaseAdminClient();

  const contents: Record<string, unknown>[] = [
    { role: "user", parts: [{ text: `${QA_SYSTEM_PREAMBLE}\n\nPregunta del usuario: ${question}` }] },
  ];

  // Bounded tool loop: this model often wants several lookups (e.g. one
  // filtered by Lima AND one unfiltered, emitted together, or a second
  // search after seeing the first), so a single round left it without a
  // text answer. Each round executes every functionCall in the turn.
  for (let round = 0; round < MAX_TOOL_ROUNDS; round++) {
    const resp = await geminiGenerateContent(contents, TOOLS);
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
        const rows = await runTool(supabase, name, args ?? {});
        response = { count: rows.length, resultados: rows.map(formatRowLine) };
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
  const final = await geminiGenerateContent(contents, TOOLS, { functionCallingConfig: { mode: "NONE" } });
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

async function handleResumen(chatId: number | string): Promise<void> {
  const supabase = supabaseAdminClient();

  const [nuevas, activas] = await Promise.all([
    fetchNuevasEstaSemana(supabase),
    fetchActivas(supabase),
  ]);

  if (nuevas.length === 0 && activas.length === 0) {
    await sendTelegramMessage(
      chatId,
      "No hay convocatorias nuevas esta semana ni activas en este momento.",
    );
    return;
  }

  const prompt = buildPrompt(nuevas, activas);
  const summary = await callGemini(prompt);
  await sendTelegramMessage(chatId, summary);
}

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
    const isResumenCommand = trimmedText.toLowerCase() === "/resumen";

    if (trimmedText === "") {
      // No text (e.g. a sticker/photo update) — nothing to do.
      return new Response("ok", { status: 200 });
    }

    if (trimmedText.startsWith("/") && !isResumenCommand) {
      // Any other bot command (e.g. /start) is ignored, not treated as a
      // free-form question.
      return new Response("ok", { status: 200 });
    }

    if (chatId === undefined || chatId === null) {
      console.error("telegram-resumen: update had no chat id", JSON.stringify(update).slice(0, 500));
      return new Response("ok", { status: 200 });
    }

    try {
      if (isResumenCommand) {
        await handleResumen(chatId);
      } else {
        await handleFreeQuestion(chatId, trimmedText);
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
