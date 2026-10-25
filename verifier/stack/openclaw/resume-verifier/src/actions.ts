// H4: forward OpenClaw Slack interactive-handler events to POST /internal/actions
// (design doc 8.4, 8.5). No OpenClaw SDK runtime imports.
//
// What OpenClaw 2026.9.2 hands a Slack interactive handler (confirmed from the
// @openclaw/slack 2026.9.2 dist, extensions/slack/src/monitor/events/interactions.*):
//   * It does NOT pass the raw Slack payload. Block actions arrive as a normalized
//     `interaction` {kind: "button"|"select", actionId, blockId, value, selectedValues,
//     selectedLabels, messageTs, threadTs, triggerId, responseUrl}; modal events as
//     {kind: "view_submission"|"view_closed", callbackId, viewId, inputs, stateValues, triggerId}.
//     So the payload sent to the verifier is rebuilt in Slack's shape from those fields.
//   * OpenClaw has already called Slack's ack() by the time the handler runs, so the
//     verifier's ack_payload cannot become the ack body. Requests therefore carry
//     `X-Verifier-Ack: none`, and the service reports invalid edits in the thread instead (8.4).
//   * Modal private_metadata is NOT passed to the handler; the ids ride in the callback_id suffix.

import type { Logger, VerifierClient } from "./client.ts";

export const RV_PREFIX = "openclaw:rv:";
export const EDIT_CLAIM_CALLBACK = "openclaw:rv:edit_claim";
/** Tells the service the caller cannot deliver an ack body (OpenClaw already acked Slack). */
export const ACK_NONE_HEADERS = { "X-Verifier-Ack": "none" } as const;
export const CLAIM_MENU_ACTION = "openclaw:rv:claim_menu";

/** The slice of OpenClaw's Slack handler context this module reads. */
export interface SlackHandlerContext {
  senderId?: string;
  senderUsername?: string;
  conversationId?: string;
  threadId?: string;
  interaction?: {
    kind?: string;
    actionId?: string;
    blockId?: string;
    messageTs?: string;
    threadTs?: string;
    value?: string;
    selectedValues?: string[];
    selectedLabels?: string[];
    triggerId?: string;
    responseUrl?: string;
    callbackId?: string;
    viewId?: string;
    stateValues?: unknown;
  };
}

export interface ActionPayload {
  type: "block_actions" | "view_submission";
  user: { id: string; name: string };
  trigger_id?: string;
  channel?: { id: string };
  container?: { type: "message"; channel_id: string; message_ts?: string; thread_ts?: string };
  actions?: Array<Record<string, unknown>>;
  view?: Record<string, unknown>;
}

export function isRvInteraction(ctx: SlackHandlerContext): boolean {
  const i = ctx.interaction;
  if (!i) return false;
  if (i.kind === "view_submission" || i.kind === "view_closed") {
    return (i.callbackId ?? "").startsWith(RV_PREFIX);
  }
  return (i.actionId ?? "").startsWith(RV_PREFIX);
}

/** Build the payload for POST /internal/actions; null when the event is not for us or is a view_closed. */
export function buildActionPayload(ctx: SlackHandlerContext): ActionPayload | null {
  const i = ctx.interaction;
  if (!i || !isRvInteraction(ctx)) return null;
  const userId = ctx.senderId?.trim() || "unknown";
  const user = { id: userId, name: ctx.senderUsername?.trim() || userId };

  if (i.kind === "view_closed") return null;

  if (i.kind === "view_submission") {
    // OpenClaw does not pass the modal's private_metadata to handlers, so the service puts the ids
    // in the callback_id (openclaw:rv:edit_claim:<application_id>:<claim_id>:<claims_version>);
    // it is forwarded as-is and the service reads the ids from the suffix (8.5).
    return {
      type: "view_submission",
      user,
      ...(i.triggerId ? { trigger_id: i.triggerId } : {}),
      view: {
        ...(i.viewId ? { id: i.viewId } : {}),
        type: "modal",
        callback_id: i.callbackId ?? "",
        private_metadata: "",
        state: { values: i.stateValues ?? {} },
      },
    };
  }

  // block_actions: button (kind "button") or overflow / select (kind "select")
  const actionId = i.actionId ?? "";
  const action: Record<string, unknown> = { action_id: actionId };
  if (i.blockId) action.block_id = i.blockId;
  const selected = i.selectedValues?.[0];
  if (i.kind === "select" && selected !== undefined) {
    action.type = "overflow";
    action.selected_option = {
      text: { type: "plain_text", text: i.selectedLabels?.[0] ?? selected },
      value: selected,
    };
  } else {
    action.type = "button";
    if (i.value !== undefined) action.value = i.value;
  }
  const channelId = ctx.conversationId?.trim();
  const threadTs = i.threadTs ?? ctx.threadId;
  return {
    type: "block_actions",
    user,
    ...(i.triggerId ? { trigger_id: i.triggerId } : {}),
    ...(channelId
      ? {
          channel: { id: channelId },
          container: {
            type: "message",
            channel_id: channelId,
            ...(i.messageTs ? { message_ts: i.messageTs } : {}),
            ...(threadTs ? { thread_ts: threadTs } : {}),
          },
        }
      : {}),
    actions: [action],
  };
}

export interface ForwardResult {
  /** False when the event was not for us (caller should return handled:false so OpenClaw falls through). */
  handled: boolean;
  status?: number;
  ackPayload?: unknown;
  error?: string;
}

function describeAction(payload: ActionPayload): string {
  if (payload.type === "view_submission") return String(payload.view?.callback_id ?? "view");
  return String(payload.actions?.[0]?.action_id ?? "unknown");
}

/** Forward one interactive-handler event. Never throws. */
export async function forwardInteraction(
  ctx: SlackHandlerContext,
  client: VerifierClient,
  logger: Logger,
  timeoutMs: number,
): Promise<ForwardResult> {
  const payload = buildActionPayload(ctx);
  if (!payload) {
    // Ours but nothing to forward (view_closed) is still handled; not ours is not.
    const mine = isRvInteraction(ctx);
    if (mine) logger.info(`resume-verifier: ignoring ${ctx.interaction?.kind} for ${ctx.interaction?.callbackId}`);
    return { handled: mine };
  }
  const label = describeAction(payload);
  const res = await client.post("/internal/actions", payload, timeoutMs, { ...ACK_NONE_HEADERS });
  if (res.status === 0) {
    logger.warn(`resume-verifier: action=${label} user=${payload.user.id} forward failed: ${res.error}`);
    return { handled: true, status: 0, error: res.error };
  }
  const ack = (res.body as { ack_payload?: unknown } | undefined)?.ack_payload ?? null;
  const level = res.status >= 200 && res.status < 300 ? "info" : "warn";
  logger[level](
    `resume-verifier: action=${label} user=${payload.user.id} status=${res.status}` +
      (ack ? " ack_payload=present (not usable: OpenClaw already acked Slack)" : ""),
  );
  return { handled: true, status: res.status, ackPayload: ack ?? undefined };
}
