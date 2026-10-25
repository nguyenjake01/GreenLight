// Plugin registration, split from index.ts so tests can call it without loading the OpenClaw SDK.

import type { OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";
import { createClient, resolveConfig } from "./client.ts";
import { forwardInteraction, type SlackHandlerContext } from "./actions.ts";
import { createTools, TOOL_NAMES, type ToolContext } from "./tools.ts";

/**
 * Interactive-handler namespaces. OpenClaw routes a Slack block action by the text before the
 * first ":" of `<action_id>:<value>`, which for `openclaw:rv:*` ids is "openclaw"; modals strip a
 * leading "openclaw:" from callback_id first, so `openclaw:rv:edit_claim` routes as "rv".
 */
export const SLACK_NAMESPACES = ["openclaw", "rv"] as const;

export function register(api: OpenClawPluginApi): void {
  const config = resolveConfig(api.pluginConfig);
  const log = api.logger;
  const client = createClient(config);
  log.info(
    `resume-verifier: mode=${config.slackActions} verifierUrl=${config.verifierUrl} token=${config.token ? "set" : "MISSING"}`,
  );
  if (!config.token) {
    log.warn("resume-verifier: no token configured (plugin config `token` or INTERNAL_TOKEN); calls will get 401");
  }

  if (config.slackActions === "plugin") {
    const handler = async (raw: unknown): Promise<{ handled: boolean }> => {
      const result = await forwardInteraction(
        raw as SlackHandlerContext,
        client,
        log,
        config.actionTimeoutMs,
      );
      return { handled: result.handled };
    };
    for (const namespace of SLACK_NAMESPACES) {
      api.registerInteractiveHandler({ channel: "slack", namespace, handler });
    }
  }

  // One factory serves all tools so the tool context (asking user) is available.
  // press_button is only exposed in agent mode (Fallback A).
  api.registerTool(
    (toolCtx) => {
      const t = createTools(client, toolCtx as ToolContext);
      const tools: unknown[] = [t.getVerdict, t.recheck, t.explainClaim];
      if (config.slackActions === "agent") tools.push(t.pressButton);
      return tools as never;
    },
    { names: [...TOOL_NAMES] },
  );
}
