/**
 * Overview: the pane you land on when nothing is selected.
 *
 * The rail answers "what is running right now". This answers a *different* question — "what
 * needs my attention" — so it deliberately does not repeat the rail's contents. Showing the
 * same eight sessions twice, in two arrangements, is not information; it is a second place
 * to look at identical text.
 *
 * So the order of priority here is:
 *   1. blocked on the user   — nothing moves until you answer
 *   2. broken sessions       — you have to act on these
 *   3. running sessions      — the live board
 *   4. everything finished   — collapsed behind a disclosure, because it is history
 *
 * The four tiles partition the board exactly (running + attention + finished = total), which
 * is why a session waiting on you is counted under *needs attention* and not under *running*
 * as well: it is running, but the tile row answers "where do things stand", and one session
 * counted in two buckets makes the numbers stop adding up.
 *
 * When nothing needs attention the page says so in one line instead of padding the screen
 * with green checkmarks.
 */

import { useState } from "react";

import { IconCheck, IconChevronRight, IconFolder } from "../components/icons";
import { doingLine, isAwaitingUser, isBusy, relativeTime } from "../lib/format";
import type { Session } from "../types/api";

export function OverviewPage({
  sessions,
  onOpen,
}: {
  sessions: Session[];
  onOpen: (id: string) => void;
}) {
  const [showFinished, setShowFinished] = useState(false);

  // Waiting first, and gated on `running` inside `isAwaitingUser` so a late notification
  // cannot park a finished session here.
  const awaiting = sessions.filter(isAwaitingUser);
  const broken = sessions.filter((s) => s.status === "failed");
  const interrupted = sessions.filter((s) => s.status === "interrupted");
  const running = sessions.filter((s) => isBusy(s) && !isAwaitingUser(s));
  const finished = sessions.filter(
    (s) => s.status === "completed" || s.status === "unknown",
  );

  const needsAttention = [...awaiting, ...broken, ...interrupted];
  // Red only when something actually ended badly. A board whose only problem is a session
  // waiting on an answer is not in a bad state — it is waiting on you, which is amber.
  const endedBadly = broken.length + interrupted.length > 0;

  return (
    <div className="overview">
      <div className="stats">
        <Stat n={running.length} label="Running" tone="live" />
        <Stat
          n={needsAttention.length}
          label="Needs attention"
          tone={endedBadly ? "dead" : awaiting.length > 0 ? "warn" : undefined}
        />
        <Stat n={finished.length} label="Finished" />
        <Stat n={sessions.length} label="Total sessions" />
      </div>

      {needsAttention.length > 0 && (
        <>
          <p className="section-label">Needs attention</p>
          <div className="cards cards--attention">
            {needsAttention.map((session) => (
              <SessionCard key={session.id} session={session} onOpen={onOpen} />
            ))}
          </div>
        </>
      )}

      {running.length > 0 ? (
        <>
          <p className="section-label section-label--spaced">Running now</p>
          <div className="cards cards--running">
            {running.map((session) => (
              <SessionCard key={session.id} session={session} onOpen={onOpen} />
            ))}
          </div>
        </>
      ) : (
        needsAttention.length === 0 && <AllClear />
      )}

      {finished.length > 0 && (
        <>
          <button
            className="disclosure"
            onClick={() => setShowFinished((v) => !v)}
            aria-expanded={showFinished}
          >
            <IconChevronRight
              size={15}
              className={`disclosure__chev${showFinished ? " is-open" : ""}`}
            />
            {showFinished ? "Hide" : "Show"} {finished.length} finished session
            {finished.length === 1 ? "" : "s"}
          </button>

          {showFinished && (
            <div className="cards cards--running">
              {finished.map((session) => (
                <SessionCard key={session.id} session={session} onOpen={onOpen} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function AllClear() {
  return (
    <div className="allclear">
      <span className="allclear__mark" aria-hidden="true">
        <IconCheck size={16} />
      </span>
      <div>
        <p className="allclear__t">Nothing running, nothing broken.</p>
        <p className="allclear__b">
          Start a session from your dev machine with{" "}
          <code className="mono">cpctl session start</code> and it will show up here.
        </p>
      </div>
    </div>
  );
}

/** A metric tile. The number is the content; the label is the caption under it. */
function Stat({
  n,
  label,
  tone,
}: {
  n: number;
  label: string;
  tone?: "live" | "dead" | "warn";
}) {
  return (
    <div className={`stat${tone ? ` stat--${tone}` : ""}`}>
      <div className="stat__n">{n}</div>
      <div className="stat__l">{label}</div>
    </div>
  );
}

function SessionCard({
  session,
  onOpen,
}: {
  session: Session;
  onOpen: (id: string) => void;
}) {
  const awaiting = isAwaitingUser(session);
  /*
    A session waiting on an answer is usually quiet for a long time — that is what waiting
    looks like — so the two states collide on exactly the case that matters. Waiting wins:
    "blocked on your permission prompt" is what the agent reported, and staleness is only an
    inference from silence, so letting the inference overwrite the report would hide the one
    line that tells you what to do. Same rule as design-ui.md §2, applied to the card.
  */
  const stale = session.possibly_stale && isBusy(session) && !awaiting;

  return (
    <button
      className="card"
      data-status={session.status}
      data-stale={stale}
      data-awaiting={awaiting}
      onClick={() => onOpen(session.id)}
      title={session.title}
    >
      <div className="card__head">
        <span className="card__dot" aria-hidden="true" />
        <span className="card__agent">{session.agent_type}</span>
        <span className="card__age">{relativeTime(session.last_activity_at)}</span>
      </div>

      <h3 className="card__title">{session.title}</h3>

      <div className="card__preview">
        <span className="card__bullet" aria-hidden="true" />
        <span className="card__preview-text">
          {stale ? "No activity reported" : doingLine(session)}
        </span>
      </div>

      <div className="card__meta">
        {session.project_name && (
          /*
            The full workspace path is noise in a card: it is 60 characters of directories
            that all share a prefix, and it wraps to two lines that say less than the project
            name already on screen. The detail page is where a path belongs.
          */
          <span className="card__chip">
            <IconFolder size={13} />
            <span>{session.project_name}</span>
          </span>
        )}
        {session.exit_code !== null && (
          <span className="card__exit">exit {session.exit_code}</span>
        )}
      </div>
    </button>
  );
}
