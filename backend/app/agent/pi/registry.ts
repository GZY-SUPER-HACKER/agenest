/**
 * The registry tool: what the dashboard knows, on demand.
 *
 * `MACHINES.registered.md` in the workspace is the standing answer — which hosts exist, which
 * the user has authorized, the `ssh` line for each — and the appendix carries only an index
 * line pointing at it. This is the other half: the questions a file cannot answer, because they
 * change while nobody is rewatching it. *What is running on `lab-box` right now? Is it still
 * reporting?* Those are the ones worth a tool call.
 *
 * **How it answers, and why this way.** It runs `app.agent.hosts_view` under the interpreter
 * this service is already using, with the database opened read-only. That is deliberately not an
 * HTTP call: a route would need a credential in this process's environment, and the only
 * credential this service has is the one that drives the whole page. A read-only file handle
 * needs no token, cannot write the record, and cannot reach past this machine.
 *
 * **Nothing here is prompt text.** The output is data the model asked for, not instructions it
 * was handed — which is what lets the appendix stay one paragraph no matter how many machines
 * a deployment has.
 *
 * Loaded explicitly with `-e` by the native backend, beside `appendix.ts` and independently of
 * it. If the environment is missing the three paths below, the tool says so rather than
 * throwing: a deployment without them is a deployment where this feature is not configured,
 * which is not a broken turn.
 */

import { execFile } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const PYTHON = process.env.CSD_AGENT_PYTHON ?? "";
const DB_PATH = process.env.CSD_AGENT_DB_PATH ?? "";
const BACKEND_DIR = process.env.CSD_AGENT_BACKEND_DIR ?? "";

/** Seconds a registry query may take before it is abandoned. It reads two small tables; a
 *  timeout here means something is wedged, and a tool call that never returns is worse than one
 *  that returns a sentence. */
const TIMEOUT_MS = 10_000;

/** Cap on what the tool may return. The full session list is not the point — the model asked
 *  "what is running", and a hundred rows of it would crowd out the answer it is writing. */
const DEFAULT_LIMIT = 50;

function runRegistry(args: string[]): Promise<string> {
  return new Promise((resolve) => {
    execFile(
      PYTHON,
      ["-m", "app.agent.hosts_view", "--db", DB_PATH, ...args],
      {
        cwd: BACKEND_DIR,
        // `app` has to resolve, and the child's cwd is the agent's workspace rather than the
        // backend by default. PYTHONPATH is how the module is found without `cd`-ing anywhere.
        env: { ...process.env, PYTHONPATH: BACKEND_DIR },
        timeout: TIMEOUT_MS,
        maxBuffer: 1024 * 1024,
      },
      (error, stdout, stderr) => {
        if (stdout && stdout.trim()) {
          // The module prints a sentence on every failure path it controls, so a non-empty
          // stdout is the answer even when the exit code is unhappy.
          resolve(stdout.trim());
          return;
        }
        if (error) {
          resolve(`the registry query failed: ${stderr.trim() || String(error)}`);
          return;
        }
        resolve("the registry query returned nothing.");
      },
    );
  });
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "csd_registry",
    label: "Dashboard registry",
    description:
      "The coding-agent hosts registered with this dashboard: which are reachable, which the " +
      "user has authorized the personal agent to ssh into, and what is running on them right now.",
    promptSnippet:
      "List registered coding-agent hosts, their ssh access state, and their running sessions",
    promptGuidelines: [
      "Use csd_registry before driving a coding agent on another machine; it says whether the user has authorized ssh access to that host.",
      "Use csd_registry with action 'sessions' to see what is running right now, and never edit MACHINES.registered.md — it is generated.",
    ],
    parameters: Type.Object({
      action: Type.Optional(
        Type.Union([Type.Literal("hosts"), Type.Literal("sessions")], {
          description: "'hosts' (default) lists the registered machines; 'sessions' lists what is running now.",
        }),
      ),
      host: Type.Optional(
        Type.String({
          description: "Filter sessions to one host label, as shown by the 'hosts' action.",
        }),
      ),
      limit: Type.Optional(
        Type.Number({ description: "Maximum sessions to list (default 50)." }),
      ),
    }),

    async execute(_toolCallId, params) {
      if (!PYTHON || !DB_PATH || !BACKEND_DIR) {
        return {
          content: [
            {
              type: "text",
              text: "The registry query is not configured on this server (no CSD_AGENT_PYTHON / CSD_AGENT_DB_PATH / CSD_AGENT_BACKEND_DIR). Read MACHINES.registered.md in the workspace instead.",
            },
          ],
          details: { configured: false },
        };
      }

      const action = params.action === "sessions" ? "sessions" : "hosts";
      const args = ["--action", action];
      if (typeof params.host === "string" && params.host.trim()) {
        args.push("--host", params.host.trim());
      }
      if (typeof params.limit === "number" && Number.isFinite(params.limit)) {
        const limit = Math.max(1, Math.min(Math.floor(params.limit), 200));
        args.push("--limit", String(limit));
      } else if (action === "sessions") {
        args.push("--limit", String(DEFAULT_LIMIT));
      }

      const text = await runRegistry(args);
      return { content: [{ type: "text", text }], details: { action } };
    },
  });
}
