import { test, before, after, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { createClient } from "../src/client.ts";
import { createTools } from "../src/tools.ts";
import { startFakeServer, TOKEN, type FakeServer } from "./helpers.ts";

let server: FakeServer;
before(async () => {
  server = await startFakeServer();
});
after(() => server.stop());
beforeEach(async () => {
  await server.reset();
});

const mk = (ctx = {}, token = TOKEN, url = () => server.url) =>
  createTools(createClient({ verifierUrl: url(), token, toolTimeoutMs: 3000 }), ctx);
const text = (r: { content: Array<{ text: string }> }) => r.content[0]!.text;

test("get_verdict: GET /internal/verdict?candidate=... returns the endpoint JSON as text", async () => {
  const r = await mk().getVerdict.execute("t1", { candidate: "Henry" });
  const reqs = await server.requests();
  assert.equal(reqs.length, 1);
  assert.equal(reqs[0]!.method, "GET");
  assert.equal(reqs[0]!.path, "/internal/verdict");
  assert.deepEqual(reqs[0]!.query, { candidate: "Henry" });
  assert.equal(reqs[0]!.headers["x-verifier-token"], TOKEN);
  const parsed = JSON.parse(text(r));
  assert.equal(parsed.candidate_name, "Henry Chen");
  assert.deepEqual(parsed.other_matches, []);
  assert.equal(parsed.claims[0].claim_id, "c3");
});

test("get_verdict: other_matches reaches the agent unchanged", async () => {
  const parsed = JSON.parse(text(await mk().getVerdict.execute("t", { candidate: "Alex" })));
  assert.equal(parsed.other_matches.length, 1);
});

test("get_verdict: 404 -> 'No application found for {candidate}'", async () => {
  const r = await mk().getVerdict.execute("t", { candidate: "Nobody" });
  assert.equal(text(r), "No application found for Nobody");
});

test("recheck: POST /internal/recheck with by='slack' when the context has no name", async () => {
  const r = await mk().recheck.execute("t", { candidate: "Henry" });
  const [req] = await server.requests();
  assert.equal(req!.method, "POST");
  assert.equal(req!.path, "/internal/recheck");
  assert.deepEqual(req!.body, { candidate: "Henry", by: "slack" });
  assert.deepEqual(JSON.parse(text(r)), { application_id: "12345", status: "started" });
});

test("recheck: by = display name from the tool context, else requested_by, else slack", async () => {
  await mk({ requesterDisplayName: "Jordan" }).recheck.execute("t", { candidate: "Henry", requested_by: "ignored" });
  await mk({ requesterSenderId: "U0RECRUITER" }).recheck.execute("t", { candidate: "Henry", requested_by: "Pat" });
  await mk({ requesterSenderId: "U0RECRUITER" }).recheck.execute("t", { candidate: "Henry" });
  const bys = (await server.requests()).map((r) => r.body.by);
  assert.deepEqual(bys, ["Jordan", "Pat", "slack"]);
});

test("recheck: 404 -> 'No application found for {candidate}'", async () => {
  const r = await mk().recheck.execute("t", { candidate: "Nobody" });
  assert.equal(text(r), "No application found for Nobody");
});

test("explain_claim: GET /internal/explain with candidate and claim (URL-encoded)", async () => {
  const r = await mk().explainClaim.execute("t", { candidate: "Henry", claim: "cube solver & more" });
  const [req] = await server.requests();
  assert.equal(req!.path, "/internal/explain");
  assert.deepEqual(req!.query, { candidate: "Henry", claim: "cube solver & more" });
  const parsed = JSON.parse(text(r));
  assert.equal(parsed.label, "yellow");
  assert.match(parsed.top_reason, /3 KB|2 commits/);
  assert.equal(parsed.evidence_links[0], "https://github.com/henrychen4736/cubesolver");
});

test("explain_claim: 404 and missing params", async () => {
  assert.match(text(await mk().explainClaim.execute("t", { candidate: "Nobody", claim: "c3" })), /^No application found for Nobody, or no claim matching "c3"\./);
  await server.reset();
  assert.match(text(await mk().explainClaim.execute("t", { candidate: "Henry" })), /Missing required parameter: claim/);
  assert.match(text(await mk().getVerdict.execute("t", {})), /Missing required parameter: candidate/);
  assert.equal((await server.requests()).length, 0, "no request for invalid params");
});

test("press_button: minimal block_actions payload to /internal/actions", async () => {
  const r = await mk({ requesterSenderId: "U0RECRUITER" }).pressButton.execute("t", {
    action_id: "openclaw:rv:recheck",
    value: "12345",
  });
  assert.equal(text(r), "Done.");
  const [req] = await server.requests();
  assert.equal(req!.method, "POST");
  assert.equal(req!.path, "/internal/actions");
  assert.equal(req!.headers["x-verifier-token"], TOKEN);
  assert.deepEqual(req!.body, {
    type: "block_actions",
    user: { id: "U0RECRUITER", name: "U0RECRUITER" },
    actions: [{ type: "button", action_id: "openclaw:rv:recheck", value: "12345" }],
  });
  assert.ok(!("trigger_id" in (req!.body as object)), "no trigger_id in the agent fallback");
});

test("press_button: overflow value also sent as selected_option.value; non-rv ids rejected", async () => {
  await mk().pressButton.execute("t", { action_id: "openclaw:rv:claim_menu", value: "more_context|12345|c3" });
  const [req] = await server.requests();
  const a = req!.body.actions[0];
  assert.equal(a.value, "more_context|12345|c3");
  assert.equal(a.selected_option.value, "more_context|12345|c3");
  assert.equal(req!.body.user.id, "agent");

  await server.reset();
  const r = await mk().pressButton.execute("t", { action_id: "something:else", value: "x" });
  assert.match(text(r), /only accepts actions starting with openclaw:rv:/);
  assert.equal((await server.requests()).length, 0);
});

test("errors are one line: 500, 401, unreachable", async () => {
  const r500 = text(await mk().getVerdict.execute("t", { candidate: "boom" }));
  assert.equal(r500, "Verifier error 500 from /internal/verdict: internal.");
  const r401 = text(await mk({}, "wrong").getVerdict.execute("t", { candidate: "Henry" }));
  assert.match(r401, /bad or missing token \(401\)/);
  const down = text(await mk({}, TOKEN, () => "http://127.0.0.1:1").recheck.execute("t", { candidate: "Henry" }));
  assert.match(down, /^Verifier unreachable \(.+\)\.$/);
  for (const t of [r500, r401, down]) assert.ok(!t.includes("\n"));
});

test("tool definitions: names, required params, labels", () => {
  const t = mk();
  const params = (tool: { parameters: any }) => ({ required: tool.parameters.required, props: Object.keys(tool.parameters.properties) });
  assert.deepEqual(params(t.getVerdict), { required: ["candidate"], props: ["candidate"] });
  assert.deepEqual(params(t.explainClaim), { required: ["candidate", "claim"], props: ["candidate", "claim"] });
  assert.deepEqual(params(t.recheck).required, ["candidate"]);
  assert.deepEqual(params(t.pressButton), { required: ["action_id", "value"], props: ["action_id", "value"] });
  for (const tool of Object.values(t)) assert.ok(tool.label && tool.description);
});
