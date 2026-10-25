// Runtime copy of openclaw.plugin.json `configSchema` (a test keeps the two identical).
// OpenClaw validates the manifest copy without running plugin code; the entry also needs it,
// because definePluginEntry otherwise defaults to an empty strict schema.

export const CONFIG_SCHEMA = {
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "verifierUrl": {
      "type": "string",
      "default": "http://localhost:8200",
      "description": "Base URL of the verifier service (internal API)."
    },
    "token": {
      "type": "string",
      "description": "INTERNAL_TOKEN, sent as the X-Verifier-Token header. Falls back to the INTERNAL_TOKEN environment variable."
    },
    "slackActions": {
      "type": "string",
      "enum": [
        "plugin",
        "agent",
        "off"
      ],
      "default": "plugin",
      "description": "How Slack button clicks reach the verifier. Falls back to the SLACK_ACTIONS environment variable, then plugin."
    },
    "actionTimeoutMs": {
      "type": "integer",
      "minimum": 100,
      "maximum": 3000,
      "default": 2000,
      "description": "Timeout for POST /internal/actions."
    },
    "toolTimeoutMs": {
      "type": "integer",
      "minimum": 500,
      "maximum": 60000,
      "default": 10000,
      "description": "Timeout for the agent tool calls (verdict, recheck, explain)."
    }
  }
} as const;
