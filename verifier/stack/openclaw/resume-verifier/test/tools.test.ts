import { test, before, after, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { createClient } from "../src/client.ts";
import { createTools, RULES_LINE, asMention, verdictFacts, claimFacts } from "../src/tools.ts";
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
/** The JSON is always the last line; the rules and FACTS lines come before it. */
const data = (r: { content: Array<{ text: string }> }) => text(r).split("\n").at(-1)!;

test("get_verdict: GET /internal/verdict?candidate=... returns the endpoint JSON as text", async () => {
  const r = await mk().getVerdict.execute("t1", { candidate: "Henry" });
  const reqs = await server.requests();
  assert.equal(reqs.length, 1);
  assert.equal(reqs[0]!.method, "GET");
  assert.equal(reqs[0]!.path, "/internal/verdict");
  assert.deepEqual(reqs[0]!.query, { candidate: "Henry" });
  assert.equal(reqs[0]!.headers["x-verifier-token"], TOKEN);
  assert.ok(text(r).startsWith(RULES_LINE + "\n"), "rules line first");
  assert.match(text(r), /\nFACTS: Henry Chen: 1 partially supported\. Claims to mention: 🟡 Built cubesolver, a full Rubik's cube solver in C\+\.\.\. \(c3\): Repo is 3 KB with 2 commits on one day\.\n\{/);
  assert.equal(text(r).split("\n").length, 3, "rules, facts, JSON");
  const parsed = JSON.parse(data(r));
  assert.equal(parsed.candidate_name, "Henry Chen");
  assert.deepEqual(parsed.other_matches, []);
  assert.equal(parsed.claims[0].claim_id, "c3");
});

test("get_verdict: other_matches reaches the agent unchanged", async () => {
  const parsed = JSON.parse(data(await mk().getVerdict.execute("t", { candidate: "Alex" })));
  assert.equal(parsed.other_matches.length, 1);
});

test("get_verdict: 404 -> 'No application found for {candidate}'", async () => {
  const r = await mk().getVerdict.execute("t", { candidate: "Nobody" });
  assert.equal(text(r), "No application found for Nobody");
});

test("rules line: never call anyone fake, no hiring advice, five lines, labels", () => {
  assert.ok(!RULES_LINE.includes("\n"));
  for (const needle of ["Answer only", "fake", "hire", "Five lines", "🟢", "🟡", "🔴", "⚪"]) {
    assert.ok(RULES_LINE.includes(needle), `rules line mentions ${needle}`);
  }
});

test("asMention: Slack ids become <@id>; names pass through; empty stays empty", () => {
  assert.equal(asMention("U0RECRUITER"), "<@U0RECRUITER>");
  assert.equal(asMention("slack:U0RECRUITER"), "<@U0RECRUITER>");
  assert.equal(asMention("W123ABC"), "<@W123ABC>");
  assert.equal(asMention("team:T01ABC:user:U0RECRUITER"), "<@U0RECRUITER>");
  assert.equal(asMention("Jordan Lee"), "Jordan Lee");
  assert.equal(asMention(undefined), "");
  assert.equal(asMention("  "), "");
});

test("recheck: POST /internal/recheck with by='someone' when the context has no name or id", async () => {
  const r = await mk().recheck.execute("t", { candidate: "Henry" });
  const [req] = await server.requests();
  assert.equal(req!.method, "POST");
  assert.equal(req!.path, "/internal/recheck");
  assert.deepEqual(req!.body, { candidate: "Henry", by: "someone" });
  assert.match(text(r), /^RULES: /);
  assert.match(text(r), /Reply exactly: "Re-checking Henry\. The card will update in place\."/);
  assert.deepEqual(JSON.parse(text(r).split("\n").at(-1)!), { application_id: "12345", status: "started" });
});

test("recheck: by = display name, else the sender id as a <@mention>, else requested_by, else someone", async () => {
  await mk({ requesterDisplayName: "Jordan" }).recheck.execute("t", { candidate: "Henry", requested_by: "ignored" });
  await mk({ requesterSenderId: "U0RECRUITER" }).recheck.execute("t", { candidate: "Henry", requested_by: "Pat" });
  await mk({ requesterSenderId: "U0RECRUITER" }).recheck.execute("t", { candidate: "Henry" });
  await mk({}).recheck.execute("t", { candidate: "Henry", requested_by: "Pat" });
  await mk({}).recheck.execute("t", { candidate: "Henry" });
  const bys = (await server.requests()).map((r) => r.body.by);
  assert.deepEqual(bys, ["Jordan", "<@U0RECRUITER>", "<@U0RECRUITER>", "Pat", "someone"]);
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
  assert.ok(text(r).startsWith("RULES: "));
  const parsed = JSON.parse(data(r));
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

test("the three question tools are direct-only so Tool Search never hides them from the model", () => {
  const t = mk();
  for (const tool of [t.getVerdict, t.recheck, t.explainClaim]) {
    assert.equal((tool as { catalogMode?: string }).catalogMode, "direct-only", tool.name);
  }
});

test("verdictFacts: real-service shape (supported/partial/contradicted/no_evidence) -> tally and notable claims", () => {
  const body = {
    candidate_name: "Aidan Hong",
    claims: [
      { claim_id: "c3", source_text: "quiz (TypeScript, 2025): Expo mobile app" },
      { claim_id: "c4", source_text: "portfolio-aidan-hong (JavaScript, 2021): template" },
      { claim_id: "c1", source_text: "care360 (JavaScript, 2025): site" },
    ],
    verdict: {
      claims: [
        { claim_id: "c1", label: "supported", top_reason: "4 of 4 commits" },
        { claim_id: "c3", label: "contradicted", top_reason: "No Python code in the repo" },
        { claim_id: "c4", label: "partial", top_reason: "Created Mar 2024; claim says 2021" },
        { claim_id: "c10", label: "no_evidence", top_reason: "Not verifiable from public code" },
      ],
    },
    other_matches: [],
  };
  assert.equal(
    verdictFacts(body),
    "FACTS: Aidan Hong: 1 supported, 1 partially supported, 1 contradicted, 1 no public evidence. " +
      "Claims to mention: 🔴 quiz (c3): No Python code in the repo | 🟡 portfolio-aidan-hong (c4): Created Mar 2024; claim says 2021",
  );
  assert.match(verdictFacts({ ...body, other_matches: [{ candidate_name: "Aidan Hong" }] }), /other_matches is not empty: ask which candidate/);
});

test("verdictFacts / claimFacts: odd shapes give an empty string, never throw", () => {
  for (const b of [undefined, null, "x", 5, [], {}, { verdict: {} }, { verdict: { claims: "no" } }]) {
    assert.equal(verdictFacts(b), "");
  }
  for (const b of [undefined, null, "x", {}, { label: "mystery" }]) assert.equal(claimFacts(b), "");
});

test("explain_claim: FACTS line carries label, top_reason and the run check's reason", () => {
  assert.equal(
    claimFacts({
      claim_id: "c3",
      source_text: "quiz (TypeScript, 2025): Expo app",
      label: "supported",
      top_reason: "3 of 3 commits by the candidate",
      checks: [
        { check: "commit_share", reason: "3 of 3 commits by the candidate" },
        { check: "run", reason: "Syntax check passed; couldn't test the behavior here" },
      ],
    }),
    "FACTS: 🟢 quiz (c3) is supported: 3 of 3 commits by the candidate. Run check: Syntax check passed; couldn't test the behavior here.",
  );
});
