/**
 * The provider override for Pi, driven by the settings panel.
 *
 * A deployment this page runs can point its agent at a provider Pi knows nothing about — an
 * OpenAI-compatible proxy, a company gateway — or at a familiar provider through a different
 * endpoint. Pi's command line cannot say any of that: `--provider`/`--model` name a catalogue
 * entry, and the only environment override it honours is Azure's. Its extension API can:
 * `registerProvider` adds a provider, overrides an existing one's URL, or replaces its model
 * list, and it lands before `--provider` is resolved (verified against `pi 0.84.1`).
 *
 * **Nothing here is a credential.** `CSD_AGENT_PROVIDER_CONFIG` names a JSON file the backend
 * writes — provider, base URL, API kind, model catalogue — and the key is named *by variable*
 * (`apiKeyEnv`) so Pi resolves it from the child's environment. Writing the key into this file
 * would put a second copy on disk, and putting it on the command line would publish it in `ps`.
 *
 * The rejected alternative is writing `~/.pi/agent/models.json`: that is Pi's own configuration
 * for the user's own `pi`, and a service that edits it changes a program it is merely a client
 * of. One extension, one file we own, removed the moment it is not needed.
 *
 * A missing or malformed file is silence rather than an exception: the backend decides when to
 * load this extension, and a deployment that cleared its base URL should get Pi's own catalog
 * back, not a broken turn (docs/design.md §26).
 */

import { readFileSync } from "node:fs";

const CONFIG_PATH = process.env.CSD_AGENT_PROVIDER_CONFIG ?? "";

interface ModelConfig {
  id: string;
  name?: string;
  contextWindow: number;
  maxTokens: number;
}

interface ProviderFile {
  provider: string;
  baseUrl?: string;
  /** The *name* of the variable holding the key, never the key itself. */
  apiKeyEnv?: string;
  api?: string;
  models?: ModelConfig[];
}

function readConfig(): ProviderFile | null {
  if (!CONFIG_PATH) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(CONFIG_PATH, "utf-8"));
  } catch {
    // Absent is the normal case for a deployment that configures nothing; unreadable is a bug
    // the backend already logged when it wrote the file.
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const config = parsed as ProviderFile;
  return typeof config.provider === "string" && config.provider ? config : null;
}

export default function (pi: any) {
  const config = readConfig();
  if (config === null) return;

  const registration: Record<string, unknown> = {};
  if (config.baseUrl) registration.baseUrl = config.baseUrl;
  if (config.apiKeyEnv) {
    // Pi interpolates `$NAME` from its own environment. A variable that is not set resolves to
    // nothing, which Pi reports as a missing credential — the same sentence a hand-run `pi`
    // would give, which is what makes it actionable.
    registration.apiKey = `$${config.apiKeyEnv}`;
  }

  if (config.models && config.models.length > 0) {
    // A model list *replaces* the provider's catalogue, so it is only sent when the backend
    // decided the user is defining one. The alternative — always defining a model — would
    // discard Pi's knowledge of a built-in model (reasoning, input kinds, cost) on the strength
    // of a form that only asked for an endpoint.
    registration.api = config.api ?? "openai-completions";
    registration.models = config.models.map((model) => ({
      id: model.id,
      name: model.name ?? model.id,
      reasoning: false,
      input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: model.contextWindow,
      maxTokens: model.maxTokens,
    }));
  }

  pi.registerProvider(config.provider, registration);
}
