// H5: agent tools backed by the verifier's internal API (design doc 8.4).
// No OpenClaw SDK runtime imports; `typebox` is the only runtime dependency.

import { Type } from "typebox";
import type { VerifierClient, HttpResult } from "./client.ts";
import { CLAIM_MENU_ACTION, RV_PREFIX } from "./actions.ts";

export interface ToolResult {
  content: Array<{ type: "text"; text: string }>;
  details: unknown;
}

/** Subset of OpenClaw's OpenClawPluginToolContext the tools read. */
export interface ToolContext {
  requesterSenderId?: string;
  /** Not in the 2026.9.2 SDK types; used if a future version provides it. */
  requesterDisplayName?: string;
}

function textResult(text: string, details: unknown = text): ToolResult {
  return { content: [{ type: "text", text }], details };
}

/**
 * One reminder line placed before the data of every verifier tool result, so the model sees the
 * answering rules right next to the facts it must answer from (the always-on rules in AGENTS.md
 * and the skill are easy for a small model to skip).
 */
export const RULES_LINE =
  "RULES: Answer only from the data below; if a fact is not there, say you don't have it. " +
  "Labels: 🟢 supported, 🟡 partially supported, 🔴 contradicted, ⚪ no public evidence. " +
  "Never call anyone fake, dishonest or fraudulent; never say whether to hire or reject (the human decides). " +
  "Say only what the FACTS line or the JSON states: do not say other claims 'check out' or are 'genuine' or 'real', and give no opinion of the person. " +
  "Report the run check exactly as its reason says. " +
  "Start an answer about one claim with its label emoji; start an answer about a whole candidate with the name or the disclaimer, not an emoji. " +
  "Five lines or fewer, plain sentences, no headers, no tables. Data:";

/**
 * Prepend the rules line (and optional extra) to a successful result's text; `details` stays the
 * raw data. One-line outcomes (no application, errors) are returned unchanged.
 */
export function withRules(res: ToolResult, extra = ""): ToolResult {
  const first = res.content[0];
  if (!first || typeof res.details !== "object" || res.details === null) return res;
  const lead = extra ? `${RULES_LINE}\n${extra}\n` : `${RULES_LINE}\n`;
  return { ...res, content: [{ type: "text", text: lead + first.text }] };
}

/**
 * Slack user id -> "<@Uxxxx>", which Slack renders as the person's name. Accepts a bare id or a
 * prefixed form ("slack:U0123", "team:T01:user:U0123"); anything else is returned trimmed.
 */
export function asMention(senderId: string | undefined | null): string {
  const raw = (senderId ?? "").trim();
  const last = raw.split(":").at(-1)!.trim();
  if (/^[UW][A-Z0-9]{4,}$/.test(last)) return `<@${last}>`;
  return raw;
}

// ---- Facts line: the verdict's tally and its non-green claims, pre-digested ---------------------
// The verdict JSON is large and a 35B model misread it ("8 supported" when the tally said 7).
// This line is computed only from the JSON that follows it.

type Level = "supported" | "partial" | "contradicted" | "no_evidence";
const LEVELS: Array<{ level: Level; emoji: string; words: string; aliases: string[] }> = [
  { level: "supported", emoji: "🟢", words: "supported", aliases: ["supported", "green"] },
  { level: "partial", emoji: "🟡", words: "partially supported", aliases: ["partial", "partially_supported", "yellow"] },
  { level: "contradicted", emoji: "🔴", words: "contradicted", aliases: ["contradicted", "red"] },
  { level: "no_evidence", emoji: "⚪", words: "no public evidence", aliases: ["no_evidence", "white"] },
];
const levelOf = (label: unknown) => LEVELS.find((l) => l.aliases.includes(String(label ?? "").toLowerCase()));
type Json = Record<string, unknown>;
const isObj = (v: unknown): v is Json => typeof v === "object" && v !== null && !Array.isArray(v);

/** "quiz (TypeScript, 2025): Expo app" -> "quiz"; otherwise the first 50 characters. */
function claimName(sourceText: unknown): string {
  const t = typeof sourceText === "string" ? sourceText.trim() : "";
  const m = /^(.+?) \(/.exec(t);
  return (m ? m[1]! : t.length > 50 ? `${t.slice(0, 50)}...` : t) || "claim";
}

/** One "FACTS:" line for a get_verdict body, or "" when the body has an unexpected shape. */
export function verdictFacts(body: unknown): string {
  if (!isObj(body)) return "";
  const verdict = body.verdict;
  const claims = isObj(verdict) && Array.isArray(verdict.claims) ? (verdict.claims as unknown[]) : [];
  if (claims.length === 0) return "";
  const names = new Map<string, string>();
  if (Array.isArray(body.claims)) {
    for (const c of body.claims) if (isObj(c) && typeof c.claim_id === "string") names.set(c.claim_id, claimName(c.source_text));
  }
  const counts = new Map<Level, number>();
  const notable: string[] = [];
  for (const c of claims) {
    if (!isObj(c)) continue;
    const lv = levelOf(c.label);
    if (!lv) continue;
    counts.set(lv.level, (counts.get(lv.level) ?? 0) + 1);
    if (lv.level === "contradicted" || lv.level === "partial") {
      const id = String(c.claim_id ?? "");
      notable.push(`${lv.emoji} ${names.get(id) ?? id} (${id}): ${String(c.top_reason ?? "").trim()}`);
    }
  }
  const tally = LEVELS.filter((l) => counts.get(l.level)).map((l) => `${counts.get(l.level)} ${l.words}`).join(", ");
  const name = typeof body.candidate_name === "string" ? body.candidate_name : "Candidate";
  const shown = notable.slice(0, 6).join(" | ");
  const more = notable.length > 6 ? ` | and ${notable.length - 6} more` : "";
  const others = Array.isArray(body.other_matches) && body.other_matches.length > 0
    ? " NOTE: other_matches is not empty: ask which candidate was meant before answering."
    : "";
  return `FACTS: ${name}: ${tally || "no claims"}.${notable.length ? ` Claims to mention: ${shown}${more}` : " No contradicted or partial claims."}${others}`;
}

/** One "FACTS:" line for an explain_claim body, or "". */
export function claimFacts(body: unknown): string {
  if (!isObj(body)) return "";
  const lv = levelOf(body.label);
  if (!lv) return "";
  const run = Array.isArray(body.checks)
    ? (body.checks as unknown[]).find((c) => isObj(c) && (c.check === "run" || c.name === "run"))
    : undefined;
  const runText = isObj(run) && typeof run.reason === "string" ? ` Run check: ${run.reason}.` : "";
  return `FACTS: ${lv.emoji} ${claimName(body.source_text)} (${String(body.claim_id ?? "")}) is ${lv.words}: ${String(body.top_reason ?? "").trim()}.${runText}`;
}

/** Map an HTTP result to tool output: JSON text on success, one line otherwise. */
export function formatResult(
  res: HttpResult,
  path: string,
  candidate: string,
  notFoundSuffix = "",
): ToolResult {
  if (res.status === 0) return textResult(`Verifier unreachable (${res.error ?? "no response"}).`);
  if (res.status === 404) return textResult(`No application found for ${candidate}${notFoundSuffix}`);
  if (res.status === 401) return textResult("Verifier rejected the request: bad or missing token (401).");
  if (res.status >= 200 && res.status < 300) {
    const text = res.body === undefined ? `OK (${res.status})` : JSON.stringify(res.body);
    return textResult(text, res.body);
  }
  const reason = (res.body as { error?: unknown } | undefined)?.error;
  return textResult(
    `Verifier error ${res.status} from ${path}${typeof reason === "string" ? `: ${reason}` : ""}.`,
  );
}

function requireParam(params: unknown, key: string): string | ToolResult {
  const v = (params as Record<string, unknown> | null | undefined)?.[key];
  if (typeof v === "string" && v.trim()) return v.trim();
  return textResult(`Missing required parameter: ${key}.`);
}

/**
 * With `tools.toolSearch` on (NemoClaw's local-model setup), catalog tools are hidden behind
 * tool_search / tool_describe / tool_call and a 35B model often never looks for them: it answered
 * "why is cubesolver yellow?" from general knowledge. "direct-only" keeps them in the model's
 * tool list.
 */
const DIRECT = { catalogMode: "direct-only" } as const;

export function createTools(client: VerifierClient, ctx: ToolContext = {}) {
  const getVerdict = {
    name: "get_verdict",
    ...DIRECT,
    label: "Get verdict",
    description:
      "Look up the latest verification verdict for a candidate's application (labels, per-claim checks, evidence links, other_matches). " +
      "Pass the candidate's name or application id. Call this for ANY question about a candidate, applicant, resume, project or repo, " +
      "claim, label or color (green/yellow/red/white), or verdict. Never answer such questions from memory. " +
      "If the user did not name a candidate, ask which one instead of calling.",
    parameters: Type.Object({
      candidate: Type.String({ description: "Candidate name or application id, e.g. 'Henry' or '12345'." }),
    }),
    async execute(_id: string, params: unknown): Promise<ToolResult> {
      const candidate = requireParam(params, "candidate");
      if (typeof candidate !== "string") return candidate;
      const res = formatResult(await client.get("/internal/verdict", { candidate }), "/internal/verdict", candidate);
      return withRules(res, verdictFacts(res.details));
    },
  };

  const recheck = {
    name: "recheck",
    ...DIRECT,
    label: "Re-check candidate",
    description:
      "Start a re-check of a candidate's application. The Slack card updates in place when it finishes. " +
      "Pass the candidate's name or application id.",
    parameters: Type.Object({
      candidate: Type.String({ description: "Candidate name or application id." }),
      requested_by: Type.Optional(
        Type.String({ description: "Display name of the Slack user asking, only if known. Omit otherwise." }),
      ),
    }),
    async execute(_id: string, params: unknown): Promise<ToolResult> {
      const candidate = requireParam(params, "candidate");
      if (typeof candidate !== "string") return candidate;
      const asked = (params as { requested_by?: unknown }).requested_by;
      // Display name if the context has one, else the Slack sender id as a mention, else the
      // model-supplied name, else "someone" (never "slack": the card line would read "started by slack").
      const by =
        ctx.requesterDisplayName?.trim() ||
        asMention(ctx.requesterSenderId) ||
        (typeof asked === "string" && asked.trim() ? asked.trim() : "") ||
        "someone";
      const res = formatResult(await client.post("/internal/recheck", { candidate, by }), "/internal/recheck", candidate);
      return withRules(
        res,
        `Reply exactly: "Re-checking ${candidate}. The card will update in place." Add nothing else.`,
      );
    },
  };

  const explainClaim = {
    name: "explain_claim",
    ...DIRECT,
    label: "Explain claim",
    description:
      "Explain one claim's label for a candidate: top_reason, checks, notes, flags, evidence links and the run-check outcome. " +
      "Call this for \"why is <claim> <color>?\" questions once the candidate is known. " +
      "`claim` is a claim id (e.g. 'c3') or text from the resume (e.g. 'cubesolver').",
    parameters: Type.Object({
      candidate: Type.String({ description: "Candidate name or application id." }),
      claim: Type.String({ description: "Claim id or a distinctive piece of the claim text." }),
    }),
    async execute(_id: string, params: unknown): Promise<ToolResult> {
      const candidate = requireParam(params, "candidate");
      if (typeof candidate !== "string") return candidate;
      const claim = requireParam(params, "claim");
      if (typeof claim !== "string") return claim;
      const res = formatResult(
        await client.get("/internal/explain", { candidate, claim }),
        "/internal/explain",
        candidate,
        // The service answers 404 for an unknown candidate and for an unknown claim alike.
        `, or no claim matching "${claim}". Call get_verdict to list the candidate's claims.`,
      );
      return withRules(res, claimFacts(res.details));
    },
  };

  const pressButton = {
    name: "press_button",
    label: "Press verifier button",
    description:
      "Fallback button routing. When a 'Slack interaction' event has an action starting with 'openclaw:rv:', " +
      "call this with exactly that action id and value. Do not reply afterwards.",
    parameters: Type.Object({
      action_id: Type.String({ description: "The Slack action id, e.g. 'openclaw:rv:recheck'." }),
      value: Type.String({ description: "The action's value, e.g. '12345' or 'more_context|12345|c3'." }),
    }),
    async execute(_id: string, params: unknown): Promise<ToolResult> {
      const actionId = requireParam(params, "action_id");
      if (typeof actionId !== "string") return actionId;
      if (!actionId.startsWith(RV_PREFIX)) {
        return textResult(`press_button only accepts actions starting with ${RV_PREFIX}.`);
      }
      const rawValue = (params as { value?: unknown }).value;
      const value = typeof rawValue === "string" ? rawValue : "";
      const userId = ctx.requesterSenderId?.trim() || "agent";
      const action: Record<string, unknown> = { type: "button", action_id: actionId, value };
      if (actionId === CLAIM_MENU_ACTION) {
        // Overflow clicks carry the choice in selected_option.value; send both so either reading works.
        action.type = "overflow";
        action.selected_option = { value };
      }
      const payload = {
        type: "block_actions",
        user: { id: userId, name: ctx.requesterDisplayName?.trim() || userId },
        actions: [action],
      };
      const res = await client.post("/internal/actions", payload);
      if (res.status >= 200 && res.status < 300) return textResult("Done.", res.body);
      return formatResult(res, "/internal/actions", actionId);
    },
  };

  return { getVerdict, recheck, explainClaim, pressButton };
}

export const TOOL_NAMES = ["get_verdict", "recheck", "explain_claim", "press_button"] as const;
