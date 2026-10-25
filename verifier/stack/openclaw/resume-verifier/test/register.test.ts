import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";
import { register } from "../src/register.ts";
import { resolveConfig } from "../src/client.ts";
import { CONFIG_SCHEMA } from "../src/config-schema.ts";
import { memoryLogger } from "./helpers.ts";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(readFileSync(resolve(root, "openclaw.plugin.json"), "utf8"));
const pkg = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));

function fakeApi(pluginConfig: Record<string, unknown> | undefined) {
  const handlers: Array<{ channel: string; namespace: string; handler: (c: unknown) => unknown }> = [];
  const tools: Array<{ factory: (ctx: unknown) => any; opts: any }> = [];
  const logger = memoryLogger();
  const api = {
    id: "resume-verifier",
    pluginConfig,
    logger,
    registerInteractiveHandler: (r: any) => void handlers.push(r),
    registerTool: (factory: any, opts: any) => void tools.push({ factory, opts }),
  } as unknown as OpenClawPluginApi;
  return { api, handlers, tools, logger };
}

const toolNames = (t: { factory: (ctx: unknown) => any }) => (t.factory({}) as Array<{ name: string }>).map((x) => x.name);

test("plugin mode: handlers for slack namespaces openclaw + rv, three tools, no press_button", () => {
  const { api, handlers, tools } = fakeApi({ token: "t" });
  register(api);
  assert.deepEqual(handlers.map((h) => [h.channel, h.namespace]), [["slack", "openclaw"], ["slack", "rv"]]);
  assert.equal(tools.length, 1);
  assert.deepEqual(toolNames(tools[0]!), ["get_verdict", "recheck", "explain_claim"]);
});

test("agent mode: no interactive handlers, press_button added", () => {
  const { api, handlers, tools } = fakeApi({ token: "t", slackActions: "agent" });
  register(api);
  assert.equal(handlers.length, 0);
  assert.deepEqual(toolNames(tools[0]!), ["get_verdict", "recheck", "explain_claim", "press_button"]);
});

test("off mode: no handlers, no press_button, tools still registered", () => {
  const { api, handlers, tools } = fakeApi({ token: "t", slackActions: "off" });
  register(api);
  assert.equal(handlers.length, 0);
  assert.deepEqual(toolNames(tools[0]!), ["get_verdict", "recheck", "explain_claim"]);
});

test("registered tool names match the manifest's contracts.tools", () => {
  const { api, tools } = fakeApi({ token: "t" });
  register(api);
  assert.deepEqual([...tools[0]!.opts.names].sort(), [...manifest.contracts.tools].sort());
});

test("warns when no token is configured", () => {
  const { api, logger } = fakeApi(undefined);
  const saved = process.env.INTERNAL_TOKEN;
  delete process.env.INTERNAL_TOKEN;
  try {
    register(api);
  } finally {
    if (saved !== undefined) process.env.INTERNAL_TOKEN = saved;
  }
  assert.ok(logger.lines.some((l) => l.startsWith("warn:") && l.includes("no token")));
});

test("resolveConfig: defaults, env fallbacks, trailing slash, bad mode", () => {
  assert.deepEqual(resolveConfig(undefined, {}), {
    verifierUrl: "http://localhost:8200",
    token: "",
    slackActions: "plugin",
    actionTimeoutMs: 2000,
    toolTimeoutMs: 10000,
  });
  const c = resolveConfig({ verifierUrl: "http://x:1/" }, { INTERNAL_TOKEN: "env-tok", SLACK_ACTIONS: "AGENT" });
  assert.equal(c.verifierUrl, "http://x:1");
  assert.equal(c.token, "env-tok");
  assert.equal(c.slackActions, "agent");
  assert.equal(resolveConfig({ slackActions: "bogus" }, {}).slackActions, "plugin");
  assert.equal(resolveConfig({ slackActions: "off", token: "cfg" }, { SLACK_ACTIONS: "agent", INTERNAL_TOKEN: "env" }).slackActions, "off");
  assert.equal(resolveConfig({ token: "cfg" }, { INTERNAL_TOKEN: "env" }).token, "cfg");
});

test("manifest: id, config defaults and package.json entry points line up", () => {
  assert.equal(manifest.id, "resume-verifier");
  assert.equal(manifest.configSchema.additionalProperties, false);
  assert.equal(manifest.configSchema.properties.verifierUrl.default, "http://localhost:8200");
  assert.deepEqual(manifest.configSchema.properties.slackActions.enum, ["plugin", "agent", "off"]);
  assert.equal(manifest.activation.onStartup, true);
  assert.deepEqual(manifest.contracts.tools, ["get_verdict", "recheck", "explain_claim", "press_button"]);
  assert.deepEqual(pkg.openclaw.extensions, ["./src/index.ts"]);
  assert.deepEqual(pkg.openclaw.runtimeExtensions, ["./dist/index.js"]);
  assert.equal(pkg.type, "module");
});

test("runtime CONFIG_SCHEMA is identical to the manifest configSchema", () => {
  assert.deepEqual(JSON.parse(JSON.stringify(CONFIG_SCHEMA)), manifest.configSchema);
});

test("skill copy of the agent instructions is in sync with AGENT_INSTRUCTIONS.md", () => {
  const canonical = readFileSync(resolve(root, "AGENT_INSTRUCTIONS.md"), "utf8").trim();
  const skill = readFileSync(resolve(root, "skills/resume-verifier/SKILL.md"), "utf8");
  const body = skill.replace(/^---\n[\s\S]*?\n---\n/, "").trim();
  assert.equal(body, canonical, "run `npm run sync-skill`");
});
