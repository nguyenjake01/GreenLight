import { buildJsonPluginConfigSchema, definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { CONFIG_SCHEMA } from "./config-schema.ts";
import { register } from "./register.ts";

export default definePluginEntry({
  id: "resume-verifier",
  name: "Resume Verifier",
  description:
    "Routes Resume Verifier Slack button clicks to the verifier service and gives the agent verdict, recheck and explain tools.",
  configSchema: buildJsonPluginConfigSchema(CONFIG_SCHEMA as never),
  register,
});
