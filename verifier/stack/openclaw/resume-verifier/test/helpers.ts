import { spawn, type ChildProcess } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Logger } from "../src/client.ts";
import type { SlackHandlerContext } from "../src/actions.ts";

const here = dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = resolve(here, "../../../../..");
export const TOKEN = "test-token";

export interface Recorded {
  method: string;
  path: string;
  query: Record<string, string>;
  headers: Record<string, string>;
  body: any;
}

export interface FakeServer {
  url: string;
  proc: ChildProcess;
  requests(): Promise<Recorded[]>;
  reset(): Promise<void>;
  stop(): void;
}

/** Start tests/stack/fake_actions_server.py on a free port (--port 0). */
export async function startFakeServer(): Promise<FakeServer> {
  const script = resolve(REPO_ROOT, "tests/stack/fake_actions_server.py");
  const proc = spawn("python3", ["-I", script, "--port", "0", "--token", TOKEN], {
    stdio: ["ignore", "pipe", "inherit"],
  });
  const port = await new Promise<number>((res, rej) => {
    let buf = "";
    const t = setTimeout(() => rej(new Error("fake server did not start")), 10000);
    proc.stdout!.on("data", (d: Buffer) => {
      buf += d.toString();
      const m = /LISTENING (\d+)/.exec(buf);
      if (m) {
        clearTimeout(t);
        res(Number(m[1]));
      }
    });
    proc.on("error", rej);
    proc.on("exit", (code) => rej(new Error(`fake server exited early (${code})`)));
  });
  const url = `http://127.0.0.1:${port}`;
  return {
    url,
    proc,
    async requests() {
      return (await (await fetch(`${url}/__requests`)).json()) as Recorded[];
    },
    async reset() {
      await fetch(`${url}/__reset`, { method: "POST" });
    },
    stop() {
      proc.removeAllListeners("exit");
      proc.kill();
    },
  };
}

export function memoryLogger(): Logger & { lines: string[] } {
  const lines: string[] = [];
  const push = (level: string) => (m: string) => void lines.push(`${level}: ${m}`);
  return { lines, debug: push("debug"), info: push("info"), warn: push("warn"), error: push("error") };
}

export function fixture(name: string): any {
  return JSON.parse(readFileSync(resolve(REPO_ROOT, "fixtures/slack", name), "utf8"));
}

/**
 * Rebuild the handler context OpenClaw's Slack extension would pass for a Slack payload
 * (mirrors @openclaw/slack 2026.9.2 interactions.block-actions / interactions.modal).
 */
export function ctxFromSlackPayload(p: any, overrides: Partial<SlackHandlerContext["interaction"]> = {}): SlackHandlerContext {
  if (p.type === "view_submission") {
    return {
      senderId: p.user.id,
      conversationId: "",
      interaction: {
        kind: "view_submission",
        callbackId: p.view.callback_id,
        viewId: p.view.id,
        stateValues: p.view.state.values,
        triggerId: p.trigger_id,
        ...overrides,
      },
    };
  }
  const a = p.actions[0];
  const selected = a.selected_option?.value;
  return {
    senderId: p.user.id,
    conversationId: p.channel?.id ?? p.container?.channel_id,
    interaction: {
      kind: a.type === "button" ? "button" : "select",
      actionId: a.action_id,
      blockId: a.block_id,
      messageTs: p.container?.message_ts,
      value: a.value,
      selectedValues: selected ? [selected] : undefined,
      selectedLabels: a.selected_option?.text?.text ? [a.selected_option.text.text] : undefined,
      triggerId: p.trigger_id,
      responseUrl: p.response_url,
      ...overrides,
    },
  };
}
