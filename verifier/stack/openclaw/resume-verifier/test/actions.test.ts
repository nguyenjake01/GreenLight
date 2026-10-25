import { test, before, after, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { createClient } from "../src/client.ts";
import { buildActionPayload, forwardInteraction } from "../src/actions.ts";
import {
  ctxFromSlackPayload,
  fixture,
  memoryLogger,
  startFakeServer,
  TOKEN,
  type FakeServer,
} from "./helpers.ts";

let server: FakeServer;
before(async () => {
  server = await startFakeServer();
});
after(() => server.stop());
beforeEach(async () => {
  await server.reset();
});

function client(token = TOKEN) {
  return createClient({ verifierUrl: server.url, token, toolTimeoutMs: 5000 });
}

const lastAction = async () => {
  const reqs = (await server.requests()).filter((r) => r.path === "/internal/actions");
  assert.equal(reqs.length, 1, "exactly one /internal/actions request");
  return reqs[0]!;
};

test("Re-check button (recheck.json): forwarded with token, trigger_id, action and value", async () => {
  const log = memoryLogger();
  const res = await forwardInteraction(ctxFromSlackPayload(fixture("recheck.json")), client(), log, 2000);
  assert.equal(res.handled, true);
  assert.equal(res.status, 200);
  const r = await lastAction();
  assert.equal(r.method, "POST");
  assert.equal(r.headers["x-verifier-token"], TOKEN);
  assert.equal(r.headers["content-type"], "application/json");
  assert.equal(r.body.type, "block_actions");
  assert.equal(r.body.user.id, "U0RECRUITER");
  assert.equal(r.body.trigger_id, "1234567890.1234567890.abcdef0123456789");
  assert.equal(r.body.actions[0].action_id, "openclaw:rv:recheck");
  assert.equal(r.body.actions[0].value, "12345");
  assert.equal(r.body.channel.id, "C0HIRING");
  assert.equal(r.body.container.message_ts, "1761400000.000100");
  assert.equal(r.body.response_url, undefined, "response_url is not forwarded");
  assert.ok(log.lines.some((l) => /action=openclaw:rv:recheck user=U0RECRUITER status=200/.test(l)), log.lines.join("\n"));
});

test("overflow menu (overflow_more_context.json): selected_option.value forwarded", async () => {
  const res = await forwardInteraction(
    ctxFromSlackPayload(fixture("overflow_more_context.json")),
    client(),
    memoryLogger(),
    2000,
  );
  assert.equal(res.handled, true);
  const a = (await lastAction()).body.actions[0];
  assert.equal(a.type, "overflow");
  assert.equal(a.action_id, "openclaw:rv:claim_menu");
  assert.equal(a.block_id, "claim_c3");
  assert.equal(a.selected_option.value, "more_context|12345|c3");
  assert.equal(a.selected_option.text.text, "More context");
});

for (const id of ["openclaw:rv:open_ats", "openclaw:rv:open_pdf", "openclaw:rv:transcript"]) {
  test(`${id}: forwarded like any other openclaw:rv: action`, async () => {
    const ctx = ctxFromSlackPayload(fixture("recheck.json"), { actionId: id, value: id.endsWith("transcript") ? "12345|c3" : undefined });
    const res = await forwardInteraction(ctx, client(), memoryLogger(), 2000);
    assert.equal(res.handled, true);
    const a = (await lastAction()).body.actions[0];
    assert.equal(a.action_id, id);
  });
}

test("Edit claim submit (edit_submit.json): view forwarded, state values intact", async () => {
  const p = fixture("edit_submit.json");
  const res = await forwardInteraction(ctxFromSlackPayload(p), client(), memoryLogger(), 2000);
  assert.equal(res.handled, true);
  const b = (await lastAction()).body;
  assert.equal(b.type, "view_submission");
  assert.equal(b.view.callback_id, "openclaw:rv:edit_claim");
  assert.equal(b.view.id, "V0EDIT");
  assert.deepEqual(b.view.state.values, p.view.state.values);
  assert.equal(b.view.state.values.role.v.selected_option.value, "creator");
  assert.equal(b.view.private_metadata, "", "bare callback_id carries no metadata");
});

test("Edit claim submit with ids in callback_id: forwarded as-is, empty private_metadata", async () => {
  const p = fixture("edit_submit.json");
  const ctx = ctxFromSlackPayload(p, { callbackId: "openclaw:rv:edit_claim:12345:c3:1" });
  await forwardInteraction(ctx, client(), memoryLogger(), 2000);
  const b = (await lastAction()).body;
  assert.equal(b.view.callback_id, "openclaw:rv:edit_claim:12345:c3:1");
  assert.equal(b.view.private_metadata, "");
});

test("every forwarded interaction carries X-Verifier-Ack: none", async () => {
  await forwardInteraction(ctxFromSlackPayload(fixture("recheck.json")), client(), memoryLogger(), 2000);
  await forwardInteraction(ctxFromSlackPayload(fixture("edit_submit.json")), client(), memoryLogger(), 2000);
  const reqs = (await server.requests()).filter((r) => r.path === "/internal/actions");
  assert.equal(reqs.length, 2);
  for (const r of reqs) assert.equal(r.headers["x-verifier-ack"], "none");
});

test("prefix filter: non-rv actions and callbacks are not forwarded and not handled", async () => {
  const log = memoryLogger();
  for (const ctx of [
    ctxFromSlackPayload(fixture("recheck.json"), { actionId: "openclaw:other:thing" }),
    ctxFromSlackPayload(fixture("recheck.json"), { actionId: "rv:recheck" }),
    ctxFromSlackPayload(fixture("recheck.json"), { actionId: undefined }),
    ctxFromSlackPayload(fixture("edit_submit.json"), { callbackId: "openclaw:other:edit" }),
    { senderId: "U1" },
  ]) {
    const res = await forwardInteraction(ctx, client(), log, 2000);
    assert.equal(res.handled, false);
  }
  assert.equal((await server.requests()).filter((r) => r.path === "/internal/actions").length, 0);
});

test("view_closed for our modal: handled, nothing forwarded", async () => {
  const ctx = ctxFromSlackPayload(fixture("edit_submit.json"), { kind: "view_closed" });
  assert.equal(buildActionPayload(ctx), null);
  const res = await forwardInteraction(ctx, client(), memoryLogger(), 2000);
  assert.equal(res.handled, true);
  assert.equal((await server.requests()).length, 0);
});

test("ack_payload from the service is passed through to the caller and logged", async () => {
  const p = fixture("edit_submit.json");
  p.view.state.values.behavior.v.value = "INVALID";
  const log = memoryLogger();
  const res = await forwardInteraction(ctxFromSlackPayload(p), client(), log, 2000);
  assert.deepEqual(res.ackPayload, {
    response_action: "errors",
    errors: { behavior: "Behavior can't be INVALID" },
  });
  assert.ok(log.lines.some((l) => l.includes("ack_payload=present")));
  // null ack_payload -> undefined
  const ok = await forwardInteraction(ctxFromSlackPayload(fixture("recheck.json")), client(), log, 2000);
  assert.equal(ok.ackPayload, undefined);
});

test("wrong token: 401 logged as warn, handler does not throw", async () => {
  const log = memoryLogger();
  const res = await forwardInteraction(ctxFromSlackPayload(fixture("recheck.json")), client("nope"), log, 2000);
  assert.equal(res.handled, true);
  assert.equal(res.status, 401);
  assert.ok(log.lines.some((l) => l.startsWith("warn:") && l.includes("status=401")));
});

test("timeout: slow verifier aborts at the configured limit, handler does not throw", async () => {
  const ctx = ctxFromSlackPayload(fixture("recheck.json"));
  ctx.senderId = "U_SLOW"; // fake server sleeps 3 s for this user
  const log = memoryLogger();
  const t0 = Date.now();
  const res = await forwardInteraction(ctx, client(), log, 400);
  const dt = Date.now() - t0;
  assert.equal(res.handled, true);
  assert.equal(res.status, 0);
  assert.ok(dt >= 350 && dt < 2000, `aborted after ${dt} ms`);
  assert.ok(log.lines.some((l) => l.startsWith("warn:") && l.includes("timed out after 400 ms")), log.lines.join("\n"));
});

test("verifier down: connection refused is logged, not thrown", async () => {
  const dead = createClient({ verifierUrl: "http://127.0.0.1:1", token: TOKEN, toolTimeoutMs: 2000 });
  const log = memoryLogger();
  const res = await forwardInteraction(ctxFromSlackPayload(fixture("recheck.json")), dead, log, 2000);
  assert.equal(res.handled, true);
  assert.equal(res.status, 0);
  assert.ok(log.lines.some((l) => l.startsWith("warn:") && l.includes("forward failed")));
});
