/**
 * Host access — who the personal agent may ssh into (docs/design.md §24).
 *
 * The one place a person decides what the agent can reach. It lives in the settings sheet
 * rather than on the rail, because it is a decision rather than a reading: the rail says which
 * machines exist, and this says which of them the agent may drive.
 *
 * The flow it presents, in the order it happens:
 *
 *   a connector registers and declares it can be driven  →  `requested`
 *   the user authorizes here                             →  `pending` (a key exists, on this server)
 *   the host's connector installs the public key         →  `ready`
 *
 * `pending` is a real state and is shown as one, with what it is waiting for and how long that
 * can take: the key is installed by the host's own heartbeat, up to the timer interval away, and
 * a UI that said "authorized" while ssh still failed would be lying about the one thing this
 * screen exists to make legible.
 *
 * Self-fetching, so `SettingsSheet` needs no new props — the sheet is mounted from a page that
 * does not otherwise know about hosts.
 */

import { useCallback, useEffect, useState } from "react";

import {
  authorizeHostAccess,
  fetchHosts,
  isUnauthorized,
  revokeHostAccess,
} from "../api/client";
import type { Host } from "../types/api";
import { IconTerminal } from "./icons";

/** What this host's access state means, in the words the user needs. */
function describe(host: Host): string {
  switch (host.access_state) {
    case "ready":
      return `authorized — ${host.access_target ?? "address set"}`;
    case "pending":
      return "the key is generated; waiting for the host to install it (next heartbeat, under 5 minutes)";
    case "address_changed":
      return "the host is declaring a different address than when this was approved; re-approve it";
    case "disabled":
      return "access revoked — the host removes the key on its next heartbeat";
    case "requested":
      return "not authorized yet";
    default:
      return "this connector did not ask to be driven";
  }
}

function actionLabel(state: Host["access_state"]): string {
  if (state === "ready") return "Revoke";
  if (state === "pending" || state === "address_changed") return "Re-approve";
  return "Authorize";
}

export function HostAccessGroup() {
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  /** A revoke is destructive and taps are cheap, so it takes two. */
  const [confirming, setConfirming] = useState<string | null>(null);
  /** The public key from the last authorization, per host — for installing it by hand on a
   *  machine whose connector cannot write `authorized_keys` itself. */
  const [keys, setKeys] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      setHosts((await fetchHosts()).hosts);
      setError(null);
    } catch (exc) {
      setError(
        isUnauthorized(exc)
          ? "this page needs an access token before it can read the registry"
          : exc instanceof Error
            ? exc.message
            : "could not read the host list",
      );
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onAct = useCallback(
    async (host: Host) => {
      setBusy(host.id);
      setError(null);
      try {
        if (host.access_state === "ready" && confirming !== host.id) {
          setConfirming(host.id);
          return;
        }
        if (host.access_state === "ready") {
          const updated = await revokeHostAccess(host.id);
          setHosts((current) =>
            (current ?? []).map((h) => (h.id === updated.id ? updated : h)),
          );
          setConfirming(null);
          setKeys((current) => {
            const next = { ...current };
            delete next[host.id];
            return next;
          });
          return;
        }
        const result = await authorizeHostAccess(host.id);
        setHosts((current) =>
          (current ?? []).map((h) => (h.id === result.host.id ? result.host : h)),
        );
        if (result.public_key) {
          setKeys((current) => ({ ...current, [host.id]: result.public_key as string }));
        }
        setConfirming(null);
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : "the request failed");
      } finally {
        setBusy(null);
      }
    },
    [confirming],
  );

  const rows = (hosts ?? []).filter((h) => h.wants_access || h.access_state);

  return (
    <section className="sheet__group">
      <h3 className="sheet__label">Host access</h3>
      <div className="sheet__card">
        {hosts === null && !error ? (
          <p className="sheet__empty">Reading the registry…</p>
        ) : rows.length === 0 ? (
          <p className="sheet__empty">
            No registered host has asked to be driven. A machine appears here after{" "}
            <code>csd-agent install</code> reports itself.
          </p>
        ) : (
          rows.map((host) => {
            const state = host.access_state;
            const key = keys[host.id];
            return (
              <div className="srow srow--static" key={host.id}>
                <span className="srow__icon">
                  <IconTerminal size={17} />
                </span>
                <span className="srow__main">
                  <span className="srow__title">
                    {host.label} · {host.agent_type}
                  </span>
                  <span className="srow__sub">
                    {confirming === host.id ? "Tap again to revoke and delete the key" : describe(host)}
                  </span>
                  {key && state !== "ready" ? (
                    <code className="hostaccess__key">{key}</code>
                  ) : null}
                </span>
                <button
                  className={
                    state === "ready" && confirming === host.id
                      ? "hostaccess__act hostaccess__act--danger"
                      : "hostaccess__act"
                  }
                  disabled={busy === host.id}
                  onClick={() => void onAct(host)}
                >
                  {busy === host.id
                    ? "…"
                    : state === "ready" && confirming === host.id
                      ? "Confirm"
                      : actionLabel(state)}
                </button>
              </div>
            );
          })
        )}
      </div>
      {error ? <p className="sheet__hint sheet__hint--bad">{error}</p> : null}
      <p className="sheet__hint">
        Authorizing gives the personal agent a full shell on that machine: it runs{" "}
        <code>ssh</code> itself, with a key this server generates. The key stays here; the host
        installs only its public half.
      </p>
    </section>
  );
}
