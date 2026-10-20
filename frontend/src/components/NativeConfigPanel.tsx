/**
 * agenest's own settings (docs/design.md §26): the model it runs on, its credential, and what it
 * may touch.
 *
 * **Only the agent this service runs itself.** A personal agent reached over a gateway keeps its
 * model and keys on its own host; a form here would change nothing, which is why the gear is
 * drawn from `AgentBackend.configurable` rather than from a name in this file.
 *
 * Three things shape the interaction, all of them from having to explain this screen in one
 * glance:
 *
 * - **An empty field is not "unset", it is "inherit".** The placeholder shows what the server's
 *   environment supplies, so "leave it alone" and "clear it" are visibly the same thing, and
 *   clearing is how you go back to the environment.
 * - **The key is never shown.** The field says whether one is stored; typing replaces it, and a
 *   second tap on Clear removes it. There is nothing to reveal, so there is nothing to leak.
 * - **A save lands on the next message.** The agent's command line is built when its process
 *   starts, and interrupting a running turn to change a model would abandon an answer — so the
 *   confirmation says which moment the change arrives at rather than pretending it is live.
 *
 * Self-fetching, like `HostAccessGroup`: the sheet is the app's one settings surface and adding
 * props to it for a section is how that surface stops being one place.
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";

import {
  checkNativeConfig,
  fetchNativeConfig,
  isAgentDisabled,
  saveNativeConfig,
} from "../api/agent";
import { isUnauthorized } from "../api/client";
import type { NativeConfig, NativeConfigPatch } from "../types/api";
import { IconChevronDown } from "./icons";

/** Pi's built-in tools, in the order the presets grow. `bash` is the line between "reads code"
 *  and "does things to the machine", which is why the presets are drawn where they are. */
const ALL_TOOLS = ["read", "grep", "find", "ls", "edit", "write", "bash"] as const;

const READ_ONLY = ["read", "grep", "find", "ls"];
const CAN_EDIT = ["read", "grep", "find", "ls", "edit", "write"];

type ToolMode = "full" | "readonly" | "edit" | "custom";

const PRESETS: Array<{ mode: Exclude<ToolMode, "custom">; label: string; tools: string[] | null }> = [
  { mode: "full", label: "Everything", tools: null },
  { mode: "readonly", label: "Read-only", tools: READ_ONLY },
  { mode: "edit", label: "Read and edit", tools: CAN_EDIT },
];

/** Which preset a stored allowlist is, so the form opens on the choice that was made. */
function modeOf(tools: string[] | null): ToolMode {
  if (tools === null) return "full";
  const key = [...tools].sort().join(",");
  for (const preset of PRESETS) {
    if (preset.tools && [...preset.tools].sort().join(",") === key) return preset.mode;
  }
  return "custom";
}

function numberOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : null;
}

/** A form value reset from a stored configuration. */
interface Draft {
  provider: string;
  model: string;
  baseUrl: string;
  apiKind: string;
  contextWindow: string;
  maxTokens: string;
  mode: ToolMode;
  tools: string[];
  trust: boolean;
}

function draftOf(config: NativeConfig): Draft {
  // The *effective* allowlist, not the stored one. Unlike a text field, a segmented control
  // cannot show "inherit" by being empty — it would claim "Everything" while the environment
  // says read-only. The placeholders do that job for provider and model; here the control shows
  // what is in force, and saving pins it (docs/design.md §26).
  const tools = config.tools ?? config.env_defaults.tools ?? null;
  return {
    provider: config.provider ?? "",
    model: config.model ?? "",
    baseUrl: config.base_url ?? "",
    apiKind: config.api_kind ?? "",
    contextWindow: config.context_window === null ? "" : String(config.context_window),
    maxTokens: config.max_tokens === null ? "" : String(config.max_tokens),
    mode: modeOf(tools),
    tools: tools ?? [...ALL_TOOLS],
    trust: config.trust_project_files,
  };
}

export function NativeConfigPanel() {
  const [config, setConfig] = useState<NativeConfig | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState(false);
  /** Typed but not saved. Empty means "leave the stored key alone". */
  const [apiKey, setApiKey] = useState("");
  /** An explicit "remove the stored key", which is different from an empty field. */
  const [clearingKey, setClearingKey] = useState(false);
  /**
   * Whether the tool control was touched at all.
   *
   * The control shows the *effective* allowlist, so on a deployment whose tools come from
   * `CSD_PI_TOOLS` it displays a choice nobody made. Sending it back on an unrelated save would
   * quietly pin an environment setting into the database — a save that only meant to change a
   * model would also freeze the permissions.
   */
  const [toolsTouched, setToolsTouched] = useState(false);
  /** Resetting erases every stored value, and taps are cheap: it takes two. */
  const [confirmReset, setConfirmReset] = useState(false);

  const load = useCallback(async () => {
    try {
      const next = await fetchNativeConfig();
      setConfig(next);
      setDraft(draftOf(next));
      setApiKey("");
      setClearingKey(false);
      setToolsTouched(false);
      setError(null);
    } catch (exc) {
      setError(
        isAgentDisabled(exc)
          ? "This server has no access token set, so the agent is switched off."
          : isUnauthorized(exc)
            ? "This page needs its token."
            : exc instanceof Error
              ? exc.message
              : "could not read the agent's configuration",
      );
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const update = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((current) => (current === null ? current : { ...current, [key]: value }));
    setNotice(null);
    setConfirmReset(false);
  };

  const chooseMode = (mode: ToolMode) => {
    setToolsTouched(true);
    update("mode", mode);
  };

  const toggleTool = (name: string) => {
    setToolsTouched(true);
    setDraft((current) => {
      if (current === null) return current;
      const tools = current.tools.includes(name)
        ? current.tools.filter((tool) => tool !== name)
        : [...current.tools, name];
      return { ...current, tools };
    });
    setNotice(null);
  };

  if (error !== null && config === null) {
    return (
      <section className="sheet__group">
        <h3 className="sheet__label">agenest</h3>
        <p className="sheet__hint sheet__hint--bad">{error}</p>
      </section>
    );
  }

  if (config === null || draft === null) {
    return (
      <section className="sheet__group">
        <p className="sheet__empty">Reading the agent's configuration…</p>
      </section>
    );
  }

  const patch = (): NativeConfigPatch => {
    const contextWindow = numberOrNull(draft.contextWindow);
    const body: NativeConfigPatch = {
      provider: draft.provider.trim() || null,
      model: draft.model.trim() || null,
      base_url: draft.baseUrl.trim() || null,
      api_kind: draft.apiKind || null,
      context_window: contextWindow,
      // Only meaningful alongside a context window: it is part of a model definition, and
      // sending it alone would describe a model nobody defined.
      max_tokens: contextWindow === null ? null : numberOrNull(draft.maxTokens),
      trust_project_files: draft.trust,
    };
    // Untouched *and* stored means "send it back unchanged"; untouched and inherited means
    // "say nothing", so an unrelated save does not freeze an environment value.
    if (toolsTouched || config.tools !== null) {
      body.tools = draft.mode === "full" ? null : draft.tools;
    }
    if (apiKey.trim()) body.api_key = apiKey.trim();
    else if (clearingKey) body.api_key = null;
    return body;
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const next = await saveNativeConfig(patch());
      setConfig(next);
      setDraft(draftOf(next));
      setApiKey("");
      setClearingKey(false);
      setToolsTouched(false);
      // The honest sentence: the process that would use this is started by the next prompt.
      setNotice("Saved. The agent uses this from its next message.");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "the save failed");
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    if (!confirmReset) {
      setConfirmReset(true);
      setNotice(null);
      return;
    }
    setConfirmReset(false);
    setSaving(true);
    setError(null);
    try {
      // Every field null is "clear", which is how the form expresses "whatever the environment
      // says" — the same rule the inputs' placeholders describe field by field.
      const next = await saveNativeConfig({
        provider: null,
        model: null,
        base_url: null,
        api_key: null,
        api_kind: null,
        context_window: null,
        max_tokens: null,
        tools: null,
        trust_project_files: false,
      });
      setConfig(next);
      setDraft(draftOf(next));
      setApiKey("");
      setClearingKey(false);
      setToolsTouched(false);
      setNotice("Cleared. The agent falls back to this server's environment.");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "the reset failed");
    } finally {
      setSaving(false);
    }
  };

  const check = async () => {
    setChecking(true);
    setError(null);
    setNotice(null);
    try {
      const result = await checkNativeConfig();
      setNotice(result.detail);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "the check failed");
    } finally {
      setChecking(false);
    }
  };

  /** The key's current state, in the one sentence that fits in a placeholder. */
  const keyState = apiKey.trim()
    ? "will replace the stored key on save"
    : clearingKey
      ? "the stored key will be removed on save"
      : config.api_key_set
        ? "a key is stored — leave blank to keep it"
        : config.api_key_env.length > 0
          ? `from the environment (${config.api_key_env.join(", ")})`
          : "not set";

  const canSave = !(draft.mode === "custom" && draft.tools.length === 0);

  return (
    <>
      {/* --- model and credential ------------------------------------ */}
      <section className="sheet__group">
        <h3 className="sheet__label">Model</h3>
        <div className="sheet__card sheet__card--fields">
          <Field label="Provider" hint={config.env_defaults.provider ? `environment: ${config.env_defaults.provider}` : "Pi's default"}>
            <input
              className="field__input"
              value={draft.provider}
              placeholder={config.env_defaults.provider ?? "e.g. deepseek"}
              spellCheck={false}
              autoCapitalize="off"
              onChange={(e) => update("provider", e.target.value)}
            />
          </Field>
          <Field label="Model" hint={config.env_defaults.model ? `environment: ${config.env_defaults.model}` : "Pi's default"}>
            <input
              className="field__input"
              value={draft.model}
              placeholder={config.env_defaults.model ?? "e.g. deepseek/deepseek-chat"}
              spellCheck={false}
              autoCapitalize="off"
              onChange={(e) => update("model", e.target.value)}
            />
          </Field>
          <Field label="API key" hint={keyState}>
            <div className="field__row">
              <input
                className="field__input"
                type="password"
                value={apiKey}
                placeholder={config.api_key_set ? "••••••••" : "not set"}
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => {
                  setApiKey(e.target.value);
                  setClearingKey(false);
                  setNotice(null);
                }}
              />
              {(config.api_key_set || clearingKey) && (
                <button
                  type="button"
                  className="ncp__btn"
                  onClick={() => {
                    setApiKey("");
                    setClearingKey(!clearingKey);
                    setNotice(null);
                  }}
                >
                  {clearingKey ? "Keep" : "Clear"}
                </button>
              )}
            </div>
          </Field>
        </div>
        <p className="sheet__hint">
          The key is stored on this server and never sent back to this page. It reaches the agent
          through its environment, not its command line.
        </p>
      </section>

      {/* --- custom endpoint ----------------------------------------- */}
      <section className="sheet__group">
        <h3 className="sheet__label">Custom endpoint</h3>
        <div className="sheet__card sheet__card--fields">
          <Field label="Base URL" hint="a proxy or a gateway; else the provider's own">
            <input
              className="field__input"
              value={draft.baseUrl}
              placeholder="https://…/v1"
              spellCheck={false}
              autoCapitalize="off"
              onChange={(e) => update("baseUrl", e.target.value)}
            />
          </Field>
          <Field label="API kind" hint="how that endpoint speaks">
            <div className="field__select">
              <select
                className="field__input"
                value={draft.apiKind}
                onChange={(e) => update("apiKind", e.target.value)}
              >
                <option value="">OpenAI-compatible (default)</option>
                <option value="openai-completions">openai-completions</option>
                <option value="openai-responses">openai-responses</option>
                <option value="anthropic-messages">anthropic-messages</option>
              </select>
              <IconChevronDown size={14} />
            </div>
          </Field>
          <Field label="Context window" hint="tokens — fill in only when defining the model here">
            <input
              className="field__input"
              inputMode="numeric"
              value={draft.contextWindow}
              placeholder="Pi's catalogue"
              onChange={(e) => update("contextWindow", e.target.value)}
            />
          </Field>
          {numberOrNull(draft.contextWindow) !== null && (
            <Field label="Max output" hint="tokens in one reply">
              <input
                className="field__input"
                inputMode="numeric"
                value={draft.maxTokens}
                placeholder="8192"
                onChange={(e) => update("maxTokens", e.target.value)}
              />
            </Field>
          )}
        </div>
        <p className="sheet__hint">
          Leave the context window empty to keep the provider's own catalogue — Pi already knows
          its models. Fill it in only for an endpoint Pi has never heard of, and the three fields
          above become that model's definition.
        </p>
      </section>

      {/* --- what the agent may touch -------------------------------- */}
      <section className="sheet__group">
        <h3 className="sheet__label">Permissions</h3>
        <div className="sheet__card sheet__card--fields">
          {/* A plain block, not `Field`: that one is a `<label>`, and a label wrapping a group
              of buttons associates with none of them. */}
          <div className="field">
            <span className="field__label">Tools</span>
            <div className="segmented">
              {PRESETS.map((preset) => (
                <button
                  key={preset.mode}
                  type="button"
                  className="segmented__opt"
                  data-active={draft.mode === preset.mode}
                  onClick={() => chooseMode(preset.mode)}
                >
                  {preset.label}
                </button>
              ))}
              <button
                type="button"
                className="segmented__opt"
                data-active={draft.mode === "custom"}
                onClick={() => chooseMode("custom")}
              >
                Custom
              </button>
            </div>
            <span className="field__hint">
              {config.tools === null && config.env_defaults.tools
                ? `from the environment (${config.env_defaults.tools.join(", ")}) — saving pins it here`
                : "what this agent may do in its workspace"}
            </span>
          </div>
          {draft.mode === "custom" && (
            <div className="toolgrid">
              {ALL_TOOLS.map((tool) => (
                <label key={tool} className="toolgrid__item">
                  <input
                    type="checkbox"
                    checked={draft.tools.includes(tool)}
                    onChange={() => toggleTool(tool)}
                  />
                  <span>{tool}</span>
                </label>
              ))}
            </div>
          )}
          <div className="srow srow--static">
            <span className="srow__main">
              <span className="srow__title">Trust the project's own files</span>
              <span className="srow__sub">
                Let `.pi` extensions, skills and settings in the workspace load
              </span>
            </span>
            <Switch
              on={draft.trust}
              onChange={(on) => update("trust", on)}
              label="Trust the project's own files"
            />
          </div>
        </div>
        <p className="sheet__hint">
          Off by default, and that is the safe half: a workspace is often just a directory of code.
          Turning it on lets whatever is in that directory extend the agent.
          {draft.mode === "custom" && draft.tools.length === 0
            ? " Pick at least one tool — an agent with none cannot answer."
            : ""}
        </p>
      </section>

      {/* --- what is not editable here -------------------------------- */}
      <section className="sheet__group">
        <h3 className="sheet__label">This deployment</h3>
        <div className="sheet__card">
          <div className="srow srow--static">
            <span className="srow__main">
              <span className="srow__title">Working directory</span>
              <span className="srow__sub">{config.workspace ?? "not configured"}</span>
            </span>
          </div>
          <div className="srow srow--static">
            <span className="srow__main">
              <span className="srow__title">In effect</span>
              <span className="srow__sub">
                {config.effective.provider ?? "no provider"} · {config.effective.model ?? "no model"}
              </span>
            </span>
          </div>
        </div>
        <p className="sheet__hint">
          The working directory decides what the agent can write *and* where its memory lives, so it
          is an environment setting rather than a toggle.
        </p>
      </section>

      {/* --- actions -------------------------------------------------- */}
      <section className="sheet__group">
        <div className="ncp__actions">
          <button
            type="button"
            className="ncp__btn ncp__btn--primary"
            disabled={saving || !canSave}
            onClick={() => void save()}
          >
            {saving ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            className="ncp__btn"
            disabled={checking || saving}
            onClick={() => void check()}
          >
            {checking ? "Checking…" : "Check"}
          </button>
          <button
            type="button"
            className="ncp__btn"
            disabled={saving}
            onClick={() => void reset()}
          >
            {confirmReset ? "Tap again to clear everything" : "Reset to environment"}
          </button>
        </div>
        {notice ? <p className="sheet__hint">{notice}</p> : null}
        {error ? <p className="sheet__hint sheet__hint--bad">{error}</p> : null}
        <p className="sheet__hint">
          Check asks Pi whether it can resolve the <strong>saved</strong> provider and model; it
          costs nothing and proves nothing about the key. The first message is what tests the
          credential.
        </p>
      </section>
    </>
  );
}

/** One labelled input. A block rather than a `srow`, because a phone needs the full width to
 *  type in and a label inside the field reads as a placeholder — which disappears exactly when
 *  the value it explains matters. */
function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span className="field__label">{label}</span>
      {children}
      {hint ? <span className="field__hint">{hint}</span> : null}
    </label>
  );
}

/**
 * A switch — the same control the sheet already uses for the process view.
 *
 * Duplicated here rather than exported from `SettingsSheet`: the sheet's copy is private to that
 * file, and exporting it would make one component's internals part of another's surface for the
 * sake of twenty lines. If a third user appears, that is when it earns a file.
 */
function Switch({
  on,
  onChange,
  label,
}: {
  on: boolean;
  onChange: (on: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      className="switch"
      data-on={on}
      onClick={() => onChange(!on)}
    >
      <span className="switch__knob" />
    </button>
  );
}
