// Regenerates skills/resume-verifier/SKILL.md from AGENT_INSTRUCTIONS.md (the canonical copy).
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const body = readFileSync(resolve(root, "AGENT_INSTRUCTIONS.md"), "utf8").trim();
const frontmatter = `---
name: resume-verifier
description: Answer questions about candidates' resume claims and start re-checks using the get_verdict, explain_claim and recheck tools.
---
`;
const out = resolve(root, "skills/resume-verifier/SKILL.md");
mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, `${frontmatter}\n${body}\n`);
console.log(`wrote ${out}`);
