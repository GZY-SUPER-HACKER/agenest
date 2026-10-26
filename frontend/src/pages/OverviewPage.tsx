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
 *   2. failed the task       — the run ended, the work did not pass
 *   3. broken sessions       — you have to act on these
 *   4. running sessions      — the live board
 *   5. idle sessions         — open, but the agent is between replies
 *   6. everything finished   — collapsed behind a disclosure, because it is history
 *
 * The tiles partition the board exactly (running + idle + attention + finished = total),
 * which is why a session waiting on you is counted under *needs attention* and not under
 * *running* as well, and why an idle one is counted under *idle* rather than *running*: it is
 * running, but the tile row answers "where do things stand", and one session counted in two
 * buckets makes the numbers stop adding up. A task-failed session works the same way even when
 * its status says `completed` — it is drawn under attention and *not* under finished.
 *
 * A dismissed session falls through to Finished (or to Running, if it is still open): the
 * reader has answered the request for attention, and the row is history rather than a problem.
 * It is still one bucket, so the arithmetic holds (docs/design.md §42).
 *
 * When nothing needs attention the page says so in one line instead of padding the screen
 * with green checkmarks.
 */

import { useState } from "react";

import { IconCheck, IconChevronRight, IconFolder } from "../components/icons";
import { doingLine, isAwaitingUser, isBusy, isIdle, isTaskCompleted, isTaskFailed, relativeTime, sessionBucket } from "../lib/format";
import type { Session } from "../types/api";

export function OverviewPage({
  sessions,
  onOpen,
}: {
  sessions: Session[];
  onOpen: (id: string) => void;
}) {
  const [showFinished, setShowFinished] = useState(false);

  /*
    One bucket per session, and the rule lives in `sessionBucket` because the detail page asks
    the same "does this need attention" question. The tile row depends on this being a
    partition: every session lands in exactly one of the four, so the numbers add up.

    A dismissed session is not gone — it fell through to Finished (or back to Running, if it
    is still open). See design.md §42.
  */
  const attention = sessions.filter((s) => sessionBucket(s) === "attention");
  const running = sessions.filter((s) => sessionBucket(s) === "running");
  const idle = sessions.filter((s) => sessionBucket(s) === "idle");
  const finished = sessions.filter((s) => sessionBucket(s) === "finished");

  // Red when something actually ended badly — a failed task counts, even if the process
  // exited 0. A board whose only problem is a session waiting on an answer is not in a bad
  // state — it is waiting on you, which is amber. Dismissed rows are not in this list, because
  // a person has already said the board no longer has a problem there.
  const endedBadly = attention.some(
    (s) => isTaskFailed(s) || s.status === "failed" || s.status === "interrupted",
  );

  return (
    <div className="overview">
      <div className="stats">
        <Stat n={running.length} label="Running" tone="live" />
        {/* Only when there is something to say. A zero tile is noise, and the point of this
            row is "where does the work stand", not a fixed dashboard skeleton. */}
        {idle.length > 0 && <Stat n={idle.length} label="Idle" />}
        <Stat
          n={attention.length}
          label="Needs attention"
          tone={endedBadly ? "dead" : attention.some(isAwaitingUser) ? "warn" : undefined}
        />
        <Stat n={finished.length} label="Finished" />
        <Stat n={sessions.length} label="Total sessions" />
      </div>

      {attention.length > 0 && (
        <>
          <p className="section-label">Needs attention</p>
          <div className="cards cards--attention">
            {attention.map((session) => (
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
        idle.length === 0 &&
        attention.length === 0 && <AllClear />
      )}

      {idle.length > 0 && (
        <>
          <p className="section-label section-label--spaced">Idle</p>
          <div className="cards cards--running">
            {idle.map((session) => (
              <SessionCard key={session.id} session={session} onOpen={onOpen} />
            ))}
          </div>
        </>
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
    A session between replies is quiet by definition, so the same collision as waiting: the
    agent reported that it finished, and that report must not be overwritten by "no activity
    reported", which is only an inference from the silence that followed.
  */
  const idle = isIdle(session);
  /*
    The agent ran, the work was checked and failed. It outranks silence for the same reason
    the other two reports do: "no activity reported" is an inference from the quiet that
    followed, and it must not overwrite the line that says the task did not pass.
  */
  const taskFailed = isTaskFailed(session);
  const taskCompleted = isTaskCompleted(session);
  const stale =
    session.possibly_stale && isBusy(session) && !awaiting && !idle && !taskFailed;

  return (
    <button
      className="card"
      data-status={session.status}
      data-stale={stale}
      data-awaiting={awaiting}
      data-idle={idle}
      data-task-failed={taskFailed}
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
        {taskFailed && <span className="card__verdict">task failed</span>}
        {taskCompleted && (
          <span className="card__verdict card__verdict--done">task done</span>
        )}
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
