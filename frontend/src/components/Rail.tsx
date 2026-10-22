/**
 * The rail: host → harness → project → session.
 *
 * The grouping decisions live in `lib/rail-tree.ts`, because that is the part that can be
 * wrong in a way nobody notices for a week — a host that quietly swallows its sessions, a
 * machine that only appears once it has work on it. What is left here is layout.
 *
 * Two rules are carried up from the session rows unchanged:
 *
 * - **A reported state and an inferred one never look alike.** A host that said it is
 *   connected gets a solid dot; one that has merely gone quiet gets the hollow "maybe
 *   offline" treatment, the same one a stale session gets. Both say so in words as well as in
 *   colour, for the same reason the status pills do.
 * - **A name with no host behind it is not a connection.** A session can name a machine with
 *   no connector ever having registered it. That still groups the work correctly, and the
 *   group gets no connection marker at all.
 *
 * A row states its status with a leading dot and the line underneath — never with a coloured
 * bar down its edge. Collapse state persists per level. A tree that reopens at full width on
 * every reload is a tree you learn to ignore, and on a phone it pushes the thing you came to
 * look at off screen.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { IconChevronDown, IconPanel } from "./icons";
import { doingLine, isAwaitingUser, isBusy, relativeTime } from "../lib/format";
import {
  buildTree,
  HOST_STATE_LABEL,
  hostState,
  type HarnessNode,
  type HostNode,
  type ProjectNode,
} from "../lib/rail-tree";
import type { ConnectionState, Host, Session, SessionStatus } from "../types/api";

const COLLAPSED_KEY = "csd.collapsed";

type Collapsed = { hosts: string[]; harnesses: string[]; projects: string[] };

function loadCollapsed(): Collapsed {
  try {
    const raw = window.localStorage.getItem(COLLAPSED_KEY);
    if (!raw) return { hosts: [], harnesses: [], projects: [] };
    const parsed = JSON.parse(raw) as Partial<Collapsed>;
    return {
      hosts: Array.isArray(parsed.hosts) ? parsed.hosts : [],
      harnesses: Array.isArray(parsed.harnesses) ? parsed.harnesses : [],
      projects: Array.isArray(parsed.projects) ? parsed.projects : [],
    };
  } catch {
    return { hosts: [], harnesses: [], projects: [] };
  }
}

function saveCollapsed(state: Collapsed): void {
  try {
    window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify(state));
  } catch {
    /* private mode: collapsing just will not persist. Not worth surfacing. */
  }
}

const FRESH_MS = 6000;

type StatusFilter = "all" | "running" | "failed" | "done";

const FILTERS: Array<{ key: StatusFilter; label: string }> = [
  { key: "running", label: "Running" },
  { key: "done", label: "Done" },
  { key: "failed", label: "Failed" },
  { key: "all", label: "All" },
];

function matchesFilter(session: Session, filter: StatusFilter): boolean {
  switch (filter) {
    case "running":
      return session.status === "running";
    case "failed":
      return session.status === "failed" || session.status === "interrupted";
    case "done":
      return session.status === "completed" || session.status === "unknown";
    default:
      return true;
  }
}

export function Rail({
  sessions,
  hosts,
  activeId,
  connection,
  onSelect,
  onCollapse,
}: {
  sessions: Session[];
  hosts: Host[];
  activeId: string | null;
  connection: ConnectionState;
  onSelect: (id: string) => void;
  onCollapse: () => void;
}) {
  const [filter, setFilter] = useState<StatusFilter>("running");
  const [collapsed, setCollapsed] = useState<Collapsed>(loadCollapsed);

  const visible = useMemo(
    () => sessions.filter((s) => matchesFilter(s, filter)),
    [sessions, filter],
  );

  // Registered hosts are shown even under a filter that hides every session on them: a filter
  // is about work, and "this machine is connected" is not work. Hiding a connected host
  // because the user tapped "Failed" would answer the wrong question.
  const tree = useMemo(() => buildTree(visible, hosts), [visible, hosts]);

  // Escape closes the drawer, which on a phone is the difference between a modal and a trap.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCollapse();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCollapse]);

  const toggle = useCallback((level: keyof Collapsed, key: string) => {
    setCollapsed((current) => {
      const next = {
        ...current,
        [level]: current[level].includes(key)
          ? current[level].filter((k) => k !== key)
          : [...current[level], key],
      };
      saveCollapsed(next);
      return next;
    });
  }, []);

  const toggleHost = useCallback((key: string) => toggle("hosts", key), [toggle]);
  const toggleHarness = useCallback((key: string) => toggle("harnesses", key), [toggle]);
  const toggleProject = useCallback((key: string) => toggle("projects", key), [toggle]);

  const totalRunning = sessions.filter(isBusy).length;
  // Counted from the registry, not the tree: a connected host whose sessions are hidden by the
  // active filter is still connected, and the footer is about machines.
  const connectedHosts = hosts.filter((h) => !h.disconnected_at && !h.possibly_stale).length;

  return (
    <>
      <div className="scrim" onClick={onCollapse} aria-hidden="true" />
      <aside className="rail" aria-label="Sessions">
        <div className="rail__head">
          <span className="rail__title">Sessions</span>
          <span className="rail__count">{sessions.length}</span>
          <span className="rail__spacer" />
          <button
            className="iconbtn"
            onClick={onCollapse}
            aria-label="Hide session list"
            title="Hide session list"
          >
            <IconPanel size={19} />
          </button>
        </div>

        <div className="rail__filter" role="tablist" aria-label="Filter sessions">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              className="chip"
              role="tab"
              aria-pressed={filter === f.key}
              aria-selected={filter === f.key}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>

        <div className="rail__body">
          {tree.length === 0 ? (
            <p className="rail__empty">
              Nothing yet. A machine appears here once its agent host reports itself, and work
              appears once an agent reports a session.
            </p>
          ) : (
            tree.map((node) => (
              <HostNodeView
                key={node.key}
                node={node}
                open={!collapsed.hosts.includes(node.key)}
                collapsedHarnesses={collapsed.harnesses}
                collapsedProjects={collapsed.projects}
                activeId={activeId}
                onToggleHost={() => toggleHost(node.key)}
                onToggleHarness={toggleHarness}
                onToggleProject={toggleProject}
                onSelect={onSelect}
              />
            ))
          )}
        </div>

        <div className="rail__foot">
          <span className={`conn conn--${connection}`}>
            <span className="conn__dot" aria-hidden="true" />
            {connection === "live" ? "live" : connection}
          </span>
          <span>
            {connectedHosts > 0 && `${connectedHosts} host${connectedHosts === 1 ? "" : "s"} · `}
            {totalRunning > 0 ? `${totalRunning} running` : "idle"}
          </span>
        </div>
      </aside>
    </>
  );
}

function HostNodeView({
  node,
  open,
  collapsedHarnesses,
  collapsedProjects,
  activeId,
  onToggleHost,
  onToggleHarness,
  onToggleProject,
  onSelect,
}: {
  node: HostNode;
  open: boolean;
  collapsedHarnesses: string[];
  collapsedProjects: string[];
  activeId: string | null;
  onToggleHost: () => void;
  onToggleHarness: (key: string) => void;
  onToggleProject: (key: string) => void;
  onSelect: (id: string) => void;
}) {
  const state = hostState(node.host);
  const label = HOST_STATE_LABEL[state];

  return (
    <section className="host" data-open={open} data-state={state}>
      <button
        className="host__head"
        onClick={onToggleHost}
        aria-expanded={open}
        aria-label={`${node.name}, ${label || "no connection reported"}, ${node.sessions.length} sessions`}
      >
        <IconChevronDown size={14} className="host__chev" />
        <span className="host__name">{node.name}</span>
        {label && <span className="host__state">{label}</span>}
        {/*
          The reminder that this machine is waiting on a decision. Deliberately not a status:
          it says what the *user* has not done, not what the machine is doing, which is why it
          is worded as a question and rendered in the quiet style rather than as a warning.
          The action lives in Settings → Host access, so this is a signpost, not a control —
          nesting a button here would fight the row's own click-to-collapse.
        */}
        {node.accessNeeds && (
          <span
            className="host__ssh"
            title="This machine asked to be driven by the agent; authorize it in Settings → Host access"
          >
            ssh?
          </span>
        )}
        <span className="host__count">{node.sessions.length}</span>
      </button>

      {/*
        The harnesses stay mounted while the host is closed — a CSS transition needs an element
        to animate, and the closing frame is the animation. The wrapper clips the height;
        `data-open` on the section drives it.
      */}
      <div className="collapse">
        <div className="collapse__in">
          {node.harnesses.map((harness) => (
            <HarnessNodeView
              key={`${node.key}/${harness.key}`}
              harness={harness}
              open={!collapsedHarnesses.includes(harness.key)}
              collapsedProjects={collapsedProjects}
              activeId={activeId}
              onToggleHarness={() => onToggleHarness(harness.key)}
              onToggleProject={onToggleProject}
              onSelect={onSelect}
            />
          ))}
        </div>
      </div>
    </section>
  );
}

function HarnessNodeView({
  harness,
  open,
  collapsedProjects,
  activeId,
  onToggleHarness,
  onToggleProject,
  onSelect,
}: {
  harness: HarnessNode;
  open: boolean;
  collapsedProjects: string[];
  activeId: string | null;
  onToggleHarness: () => void;
  onToggleProject: (key: string) => void;
  onSelect: (id: string) => void;
}) {
  const busy = harness.running > 0;

  return (
    <section className="group" data-open={open} data-busy={busy}>
      <button
        className="group__head"
        onClick={onToggleHarness}
        aria-expanded={open}
        aria-label={`${harness.name}, ${harness.sessions.length} sessions, ${harness.running} running`}
      >
        <IconChevronDown size={14} className="group__chev" />
        <span className="group__dot" aria-hidden="true" />
        <span className="group__name">{harness.name}</span>
        <span className="group__count">{harness.sessions.length}</span>
      </button>

      <div className="collapse">
        <div className="collapse__in">
          {harness.projects.map((project) => (
            <ProjectNodeView
              key={project.key}
              project={project}
              open={!collapsedProjects.includes(project.key)}
              activeId={activeId}
              onToggle={() => onToggleProject(project.key)}
              onSelect={onSelect}
            />
          ))}
          {/*
            The state this whole host level exists for: the connector is here and there is
            nothing on it. Said in words rather than left as an empty group, because an empty
            group is indistinguishable from a bug.
          */}
          {harness.connectedOnly && (
            <p className="group__none">Connected — no sessions running on it.</p>
          )}
        </div>
      </div>
    </section>
  );
}

function ProjectNodeView({
  project,
  open,
  activeId,
  onToggle,
  onSelect,
}: {
  project: ProjectNode;
  open: boolean;
  activeId: string | null;
  onToggle: () => void;
  onSelect: (id: string) => void;
}) {
  return (
    <section className="proj" data-open={open} data-busy={project.running > 0}>
      <button
        className="proj__head"
        onClick={onToggle}
        aria-expanded={open}
        aria-label={`${project.name}, ${project.sessions.length} sessions`}
      >
        <IconChevronDown size={13} className="proj__chev" />
        <span className="proj__name">{project.name}</span>
        <span className="proj__count">{project.sessions.length}</span>
      </button>

      <div className="collapse">
        <div className="collapse__in">
          <div className="proj__rows">
            {project.sessions.map((session) => (
              <SessionRow
                key={session.id}
                session={session}
                active={session.id === activeId}
                onSelect={onSelect}
              />
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function SessionRow({
  session,
  active,
  onSelect,
}: {
  session: Session;
  active: boolean;
  onSelect: (id: string) => void;
}) {
  const [fresh, setFresh] = useState(false);
  const lastEventId = session.last_event?.id ?? null;
  const seen = useRef<number | null>(null);

  // Only a *running* session pulses. A finished session receiving a late event is not news,
  // and pulsing on it would train the user to ignore the animation entirely.
  useEffect(() => {
    if (seen.current === null) {
      seen.current = lastEventId;
      return;
    }
    if (lastEventId === seen.current) return;
    seen.current = lastEventId;
    if (!isBusy(session)) return;
    setFresh(true);
    const timer = window.setTimeout(() => setFresh(false), FRESH_MS);
    return () => window.clearTimeout(timer);
  }, [lastEventId, session]);

  // Same collision as on the cards, same resolution: a session blocked on an answer is quiet
  // by definition, and the hollow "we don't know" marker would be a strange thing to draw on
  // the one row that is telling us exactly what it wants. See OverviewPage's SessionCard.
  const awaiting = isAwaitingUser(session);
  const stale = session.possibly_stale && isBusy(session) && !awaiting;

  return (
    <button
      className="row"
      data-status={session.status}
      data-stale={stale}
      data-awaiting={awaiting}
      data-fresh={fresh}
      aria-current={active}
      onClick={() => onSelect(session.id)}
      title={
        stale
          ? `${session.title} — no activity for a while; the status is not inferred`
          : session.title
      }
    >
      <span className="row__dot" aria-hidden="true" />
      <span className="row__body">
        <span className="row__top">
          <span className="row__title">{session.title}</span>
          <span className="row__age">{relativeTime(session.last_activity_at)}</span>
        </span>
        <span className="row__preview">{doingLine(session)}</span>
      </span>
    </button>
  );
}

export type { SessionStatus };
