/**
 * The per-turn appendix for Pi.
 *
 * Pi reads `--append-system-prompt` **once, when the process starts**, so an agent that is already
 * running keeps whatever it booted with — which is why the service used to restart its Pi process
 * after every Dream that changed a file (docs/design.md §18). Pi does expose a per-prompt hook, and
 * this extension uses it: `before_agent_start` fires for every user message and may replace the
 * system prompt for that run, which is the same place nanobot's `ContextBuilder.build_system_prompt()`
 * runs.
 *
 * **The text is not built here.** `CSD_AGENT_APPENDIX` names files the backend decides on
 * (`agent/appendix.py`) and `CSD_AGENT_PROGRESS_NOTES` carries the one section that is a fixed
 * string rather than a file; formatting the prompt in two languages would be two answers to "what
 * is the agent told". This file's whole job is "read those, append them" — and, because it re-reads
 * on every prompt, an edit lands on the next message instead of on the next process.
 *
 * The order is the backend's: memory, then the machines the agent may reach, then where to
 * leave a file for the user, then how to report while working.
 *
 * Loaded explicitly with `-e` by the native backend. Not `.pi/extensions/`, which project trust
 * gates in RPC mode; an explicit `-e` path is not gated. If this file is missing the backend falls
 * back to `--append-system-prompt`, which is a start-up-only snapshot — see
 * `NativeBackend.build_argv`.
 */

import { readFileSync } from "node:fs";

const APPENDIX_PATHS = (process.env.CSD_AGENT_APPENDIX ?? "")
  .split(",")
  .map((entry) => entry.trim())
  .filter((entry) => entry.length > 0);

const PROGRESS_NOTES = process.env.CSD_AGENT_PROGRESS_NOTES ?? "";

/** The index into the generated registry file (docs/design.md §24).
 *
 * A string rather than a path, unlike the appendix files: the section *is* a fixed paragraph
 * naming a path, and the thing it points at (the registered hosts) is written by the backend
 * rather than read from here.
 */
const REGISTRY_NOTE = process.env.CSD_AGENT_REGISTRY_NOTE ?? "";

/** Where to leave a file for the user (docs/design.md §29).
 *
 * A string rather than a path, unlike the appendix files: the path is concrete and per
 * conversation, and the backend knows it when it spawns this process. This half only appends
 * it — the wording lives in `agent/appendix.py`, for the same reason the other sections do.
 */
const ATTACHMENTS_NOTE = process.env.CSD_AGENT_ATTACHMENTS_NOTE ?? "";

/** One appendix file as of right now, or "" when there is nothing to read. */
function section(path: string): string {
  try {
    return readFileSync(path, "utf-8").trim();
  } catch {
    // A missing or unreadable file is "nothing to say yet", not a broken turn: a fresh deployment
    // has no memory and may have no machines file, and the agent should still answer.
    return "";
  }
}

export default function (pi: any) {
  pi.on("before_agent_start", async (event: any) => {
    // The order is the backend's, and it is the argument: memory, the operator's machines, the
    // index into the generated registry, then the instruction that has to be the freshest thing
    // in the prompt.
    const parts = [
      ...APPENDIX_PATHS.map(section),
      REGISTRY_NOTE.trim(),
      ATTACHMENTS_NOTE.trim(),
      PROGRESS_NOTES.trim(),
    ].filter((part) => part.length > 0);
    if (parts.length === 0) return;
    // Chained with whatever the prompt already carried, so an operator's own
    // `--append-system-prompt` (or another extension's) is preserved rather than replaced.
    return { systemPrompt: `${event.systemPrompt}\n\n${parts.join("\n\n")}` };
  });
}
