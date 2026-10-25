// Config parsing and the tiny HTTP client for the verifier's internal API (design doc 8.4).
// No OpenClaw SDK runtime imports here, so the node tests can load it directly.

export type SlackActionsMode = "plugin" | "agent" | "off";

export interface PluginConfig {
  verifierUrl: string;
  token: string;
  slackActions: SlackActionsMode;
  actionTimeoutMs: number;
  toolTimeoutMs: number;
}

export interface Logger {
  debug?: (message: string) => void;
  info: (message: string) => void;
  warn: (message: string) => void;
  error: (message: string) => void;
}

export const DEFAULT_VERIFIER_URL = "http://localhost:8200";
export const DEFAULT_ACTION_TIMEOUT_MS = 2000;
export const DEFAULT_TOOL_TIMEOUT_MS = 10000;

function asString(v: unknown): string | undefined {
  return typeof v === "string" && v.trim() ? v.trim() : undefined;
}

function asInt(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) && v > 0 ? Math.floor(v) : fallback;
}

/** Resolve plugin config; raw is `api.pluginConfig`, env is process.env (injectable for tests). */
export function resolveConfig(
  raw: Record<string, unknown> | undefined,
  env: Record<string, string | undefined> = process.env,
): PluginConfig {
  const cfg = raw ?? {};
  const modeRaw = (asString(cfg.slackActions) ?? asString(env.SLACK_ACTIONS) ?? "plugin").toLowerCase();
  const slackActions: SlackActionsMode =
    modeRaw === "agent" || modeRaw === "off" ? modeRaw : "plugin";
  return {
    verifierUrl: (asString(cfg.verifierUrl) ?? DEFAULT_VERIFIER_URL).replace(/\/+$/, ""),
    token: asString(cfg.token) ?? asString(env.INTERNAL_TOKEN) ?? "",
    slackActions,
    actionTimeoutMs: asInt(cfg.actionTimeoutMs, DEFAULT_ACTION_TIMEOUT_MS),
    toolTimeoutMs: asInt(cfg.toolTimeoutMs, DEFAULT_TOOL_TIMEOUT_MS),
  };
}

export type FetchLike = typeof fetch;

export interface HttpResult {
  /** HTTP status, or 0 when the request never completed (network error, timeout). */
  status: number;
  /** Parsed JSON body, or undefined if the body was empty or not JSON. */
  body: unknown;
  /** Set when status is 0: a one-line reason. */
  error?: string;
}

export interface VerifierClient {
  get(path: string, query: Record<string, string>, timeoutMs?: number): Promise<HttpResult>;
  post(
    path: string,
    payload: unknown,
    timeoutMs?: number,
    extraHeaders?: Record<string, string>,
  ): Promise<HttpResult>;
}

export function createClient(
  config: Pick<PluginConfig, "verifierUrl" | "token" | "toolTimeoutMs">,
  fetchImpl: FetchLike = fetch,
): VerifierClient {
  async function request(
    method: "GET" | "POST",
    url: string,
    payload: unknown,
    timeoutMs: number,
    extraHeaders?: Record<string, string>,
  ): Promise<HttpResult> {
    const headers: Record<string, string> = {
      "X-Verifier-Token": config.token,
      Accept: "application/json",
      ...extraHeaders,
    };
    let body: string | undefined;
    if (method === "POST") {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(payload);
    }
    try {
      const res = await fetchImpl(url, {
        method,
        headers,
        body,
        signal: AbortSignal.timeout(timeoutMs),
      });
      const text = await res.text();
      let parsed: unknown;
      if (text) {
        try {
          parsed = JSON.parse(text);
        } catch {
          parsed = undefined;
        }
      }
      return { status: res.status, body: parsed };
    } catch (err) {
      const e = err as { name?: string; message?: string; cause?: { code?: string } };
      const reason =
        e.name === "TimeoutError" || e.name === "AbortError"
          ? `timed out after ${timeoutMs} ms`
          : (e.cause?.code ?? e.message ?? "request failed");
      return { status: 0, body: undefined, error: reason };
    }
  }

  return {
    get(path, query, timeoutMs = config.toolTimeoutMs) {
      const qs = new URLSearchParams(query).toString();
      return request("GET", `${config.verifierUrl}${path}${qs ? `?${qs}` : ""}`, undefined, timeoutMs);
    },
    post(path, payload, timeoutMs = config.toolTimeoutMs, extraHeaders) {
      return request("POST", `${config.verifierUrl}${path}`, payload, timeoutMs, extraHeaders);
    },
  };
}
