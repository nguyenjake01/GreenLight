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

export function createTools(client: VerifierClient, ctx: ToolContext = {}) {
  const getVerdict = {
    name: "get_verdict",
    label: "Get verdict",
    description:
      "Look up the latest verification verdict for a candidate's application (labels, per-claim checks, evidence links, other_matches). " +
      "Pass the candidate's name or application id. Use this to answer questions about a candidate or claim.",
    parameters: Type.Object({
      candidate: Type.String({ description: "Candidate name or application id, e.g. 'Henry' or '12345'." }),
    }),
    async execute(_id: string, params: unknown): Promise<ToolResult> {
      const candidate = requireParam(params, "candidate");
      if (typeof candidate !== "string") return candidate;
      return formatResult(await client.get("/internal/verdict", { candidate }), "/internal/verdict", candidate);
    },
  };

  const recheck = {
    name: "recheck",
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
      const by =
        ctx.requesterDisplayName?.trim() ||
        (typeof asked === "string" && asked.trim() ? asked.trim() : "") ||
        "slack";
      return formatResult(
        await client.post("/internal/recheck", { candidate, by }),
        "/internal/recheck",
        candidate,
      );
    },
  };

  const explainClaim = {
    name: "explain_claim",
    label: "Explain claim",
    description:
      "Explain one claim's label for a candidate: top_reason, checks, notes, flags, evidence links and the run-check outcome. " +
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
      return formatResult(
        await client.get("/internal/explain", { candidate, claim }),
        "/internal/explain",
        candidate,
        // The service answers 404 for an unknown candidate and for an unknown claim alike.
        `, or no claim matching "${claim}". Call get_verdict to list the candidate's claims.`,
      );
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
