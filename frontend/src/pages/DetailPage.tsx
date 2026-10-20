/**
 * Session detail: identity, outcome, and the event timeline.
 *
 * Two rules from spec §7.3 are load-bearing here:
 *
 * 1. **No invented state.** Every timeline row is one reporter-provided event, verbatim.
 *    No percentages, no spinner, no guessed stage.
 * 2. **Heartbeats are not timeline entries.** The list endpoint already excludes them; one
 *    arriving live renders as a faint tick in the time margin instead — proof the stream is
 *    alive, without 40 pings burying the work.
 *
 * The close action at the bottom is the one thing here that *writes*. It is a record edit, not
 * a command — it changes a row in the dashboard's own database and executes nothing anywhere —
 * which is what keeps it out of the territory design.md §1 rules out. See `CloseAction`.
 */

import { useState } from "react";

import {
  closeSession,
  isUnauthorized,
  useSessionDetail,
} from "../api/client";
import { IconArrowLeft, IconRefresh } from "../components/icons";
import {
  absoluteTime,
  clockTime,
  eventKind,
  isAwaitingUser,
  isBusy,
  relativeTime,
  STATUS_LABEL,
} from "../lib/format";
import type { SessionEvent } from "../types/api";

export function DetailPage({
  sessionId,
  onBack,
}: {
  sessionId: string;
  onBack: () => void;
}) {
  const { detail, loading, error, reload } = useSessionDetail(sessionId);

  if (!detail && loading) {
    return <p className="empty__b" style={{ padding: 40 }}>loading…</p>;
  }

  if (error && !detail) {
    return (
      <div className="overview">
        <div className="notice notice--bad" role="alert">
          <span>Could not load this session: {error}</span>
          <button className="notice__btn" onClick={() => void reload()} aria-label="Retry">
            <IconRefresh size={15} />
          </button>
        </div>
      </div>
    );
  }

  if (!detail) return null;

  /*
    Read off `last_event`, not `events[0]`. The two usually agree, but the events list also
    carries `status_changed`, which is dashboard bookkeeping and never a display event — so
    the head of the list is not reliably the thing the session last told us. `last_event` is
    the server's answer to that question, and it is the same field the card grouped on.
  */
  const awaiting = isAwaitingUser(detail);
  // Waiting beats stale, for the same reason as on the card: a report outranks an inference.
  const stale = detail.possibly_stale && isBusy(detail) && !awaiting;

  return (
    <article className="detail">
      <header className="detail__head">
        <div style={{ minWidth: 0 }}>
          <button className="back" onClick={onBack}>
            <IconArrowLeft size={15} />
            all sessions
          </button>
          <h1 className="detail__title">{detail.title}</h1>
        </div>
        <span
          className={`status${stale ? " status--stale" : ""}`}
          data-status={detail.status}
          title={stale ? "As last reported; no activity since" : undefined}
        >
          {STATUS_LABEL[detail.status]}
        </span>
      </header>

      {awaiting && (
        /*
          The status pill above says "running", and that is not a lie — but it is not the
          whole answer either, and the bill is due on the session either way. Same move as
          the staleness notice below: say what the reported state means, rather than
          embellish the state itself.
        */
        <div className="notice notice--warn" role="status">
          <span>
            Waiting on you. The newest event below is a request, not progress — nothing moves
            until it is answered.
          </span>
        </div>
      )}

      {stale && (
        <div className="notice notice--warn" role="status">
          <span>
            No activity reported for a while. The status above is what was last reported, not
            an inference.
          </span>
        </div>
      )}

      <dl className="kv">
        <Field label="harness" value={detail.agent_type} />
        <Field label="project" value={detail.project_name ?? "—"} />
        <Field label="started" value={relativeTime(detail.started_at)} title={absoluteTime(detail.started_at)} />
        <Field
          label={isBusy(detail) ? "last activity" : "updated"}
          value={relativeTime(detail.last_activity_at)}
          title={absoluteTime(detail.last_activity_at)}
        />
        {detail.finished_at && (
          <Field
            label="finished"
            value={relativeTime(detail.finished_at)}
            title={absoluteTime(detail.finished_at)}
          />
        )}
        {detail.heartbeat_count > 0 && (
          <Field label="heartbeats" value={String(detail.heartbeat_count)} />
        )}
      </dl>

      {detail.workspace && (
        <dl className="kv" style={{ marginTop: 14 }}>
          <div style={{ gridColumn: "1 / -1" }}>
            <dt className="kv__l">workspace</dt>
            <dd className="kv__v">{detail.workspace}</dd>
          </div>
        </dl>
      )}

      {(detail.summary || detail.exit_code !== null) && (
        <section className="outcome" data-status={detail.status}>
          <div className="outcome__l">outcome</div>
          {detail.summary && <p className="outcome__s">{detail.summary}</p>}
          {detail.exit_code !== null && (
            <div className="outcome__code">exit code {detail.exit_code}</div>
          )}
        </section>
      )}

      {isBusy(detail) && (
        <CloseAction sessionId={detail.id} onClosed={() => void reload()} />
      )}

      <section className="timeline">
        <div className="timeline__head">
          <span className="timeline__l">Timeline</span>
          <span className="timeline__n">{detail.events.length}</span>
        </div>

        {detail.events.length === 0 ? (
          <p className="empty__b">No events reported yet.</p>
        ) : (
          <ol className="tl">
            {detail.events.map((event) => (
              <EventRow key={event.id} event={event} />
            ))}
          </ol>
        )}
      </section>
    </article>
  );
}

/**
 * Close a session nothing is going to report on.
 *
 * The case: a session created outside the reporting system — a hand-run `cpctl session start`,
 * a script, a test fixture that outlived its test — has no process behind it. No heartbeat
 * will ever probe it (there is no pid to look for) and no `SessionEnd` will ever fire, so it
 * sits at `running` forever. The server will not resolve that on its own, and should not:
 * silence looks the same whether the work finished, crashed, or was never real (design.md §2).
 * A person looking at the row does know, and this is how they say so.
 *
 * **Shown only while the session is busy.** The server's terminal states are sticky and
 * first-write-wins (§3), so closing a finished session applies nothing and the button would
 * appear broken. An action that cannot work should not be offered.
 *
 * **No credential is asked for here.** This ships the same token as every read — one page, one
 * token (docs/design.md §22) — so the button is the whole interaction. It is still the only
 * route in the dashboard that changes the record, and the server still refuses it while no access
 * token is configured at all; when that happens the message says so, because "set
 * CSD_DASHBOARD_TOKEN" is the fix and it is not discoverable from a bare 403.
 */
function CloseAction({
  sessionId,
  onClosed,
}: {
  sessionId: string;
  onClosed: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const close = async () => {
    const confirmed = window.confirm(
      "Close this session as interrupted?\n\n" +
        "Use this when nothing is going to report an outcome. A closed session cannot be " +
        "reopened, and this is recorded as interrupted rather than completed — the dashboard " +
        "will not claim the work finished.",
    );
    if (!confirmed) return;

    setBusy(true);
    setProblem(null);
    try {
      await closeSession(sessionId);
      onClosed();
    } catch (exc) {
      setProblem(
        isUnauthorized(exc) ? "That token was not accepted." : exc instanceof Error ? exc.message : String(exc),
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="closebox">
      <p className="closebox__why">
        Nothing is going to report an outcome for this session. If you know it is not coming
        back, close it — the entry stays, marked interrupted rather than finished.
      </p>

      <div className="closebox__form">
        <button className="closebox__btn" disabled={busy} onClick={() => void close()}>
          {busy ? "Closing…" : "Close session"}
        </button>
      </div>

      {problem && <p className="closebox__problem">{problem}</p>}
    </section>
  );
}

function Field({
  label,
  value,
  title,
}: {
  label: string;
  value: string;
  title?: string;
}) {
  return (
    <div>
      <dt className="kv__l">{label}</dt>
      <dd className="kv__v" title={title ?? value}>
        {value}
      </dd>
    </div>
  );
}

function EventRow({ event }: { event: SessionEvent }) {
  if (event.event_type === "heartbeat") {
    return (
      <li className="ev ev--tick" title="Liveness ping — not a user-visible event">
        <div className="ev__row">
          <span className="ev__t">{clockTime(event.occurred_at)}</span>
          <span className="ev__m" style={{ color: "var(--ink-4)", fontSize: 11.5 }}>
            · heartbeat
          </span>
        </div>
      </li>
    );
  }

  const kind = eventKind(event.event_type);
  const payload = event.payload ? Object.entries(event.payload) : [];

  return (
    <li className="ev" data-kind={kind}>
      <div className="ev__row">
        <span className="ev__t" title={absoluteTime(event.occurred_at)}>
          {clockTime(event.occurred_at)}
        </span>
        <span className="ev__m">{event.message}</span>
      </div>
      {payload.length > 0 && (
        <div className="ev__p">
          {payload.map(([key, value]) => (
            <code key={key}>
              {key}={typeof value === "object" ? JSON.stringify(value) : String(value)}
            </code>
          ))}
        </div>
      )}
    </li>
  );
}