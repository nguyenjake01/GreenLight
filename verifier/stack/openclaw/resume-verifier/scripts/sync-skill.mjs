// Regenerates skills/resume-verifier/SKILL.md from AGENT_INSTRUCTIONS.md (the canonical copy).
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const body = readFileSync(resolve(root, "AGENT_INSTRUCTIONS.md"), "utf8").trim();
const frontmatter = `---
name: resume-verifier
description: Use for ANY question about a job candidate or applicant, a resume, a project or repo named in a resume (e.g. cubesolver, quiz, portfolio), a claim, a label or color (green, yellow, red, white), a verdict, or "fake / honest / should we hire" questions, and for re-checks. Answer only with the get_verdict, explain_claim and recheck tools; never from general knowledge.
---
`;
const out = resolve(root, "skills/resume-verifier/SKILL.md");
mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, `${frontmatter}\n${body}\n`);
console.log(`wrote ${out}`);
