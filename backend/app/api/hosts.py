"""`/api/hosts` — the machines that run coding agents, and who may drive them.

Auth split, following `/api/sessions` exactly:

- `POST` requires the **ingest token**, because it is the only endpoint here that changes
  state from outside the machine, and the only one a connector needs.
- `GET` requires the **dashboard token**, enforced only when `CSD_DASHBOARD_TOKEN` is set —
  the same reasoning as a session read: gating reads by default would mean typing a token on
  a phone for no gained security on a private deployment, and the guard is there for the
  devices that reach it from outside the tailnet.

There is no `PATCH` and no delete. A host is not edited from the dashboard: it is present
because a connector said so, and it goes away because a connector said goodbye. Anything else
would be this service making a claim about a machine it cannot see (docs/design.md §1, §2).

**Access is the exception, and it is the point of the split** (§24). `POST /{id}/access` is how
a *person* approves ssh access to one machine, so it requires the credential that acts
(`access_header`), not the one that reports. The connector's own two routes — ask what state it
is in, and say whether the key went in — take the ingest token, because that is what a
connector has. The private key is never in a response on either side.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..agent import hosts_view
from ..auth import access_header, dashboard_header, token_header
from ..schemas import (
    HostAccessFetchOut,
    HostAccessOut,
    HostAccessReport,
    HostAccessRequest,
    HostListOut,
    HostOut,
    HostReport,
)
from ..services import host_access_service as ha
from ..services import host_service as hs
from ..services import session_service as ss
from . import deps

router = APIRouter(prefix="/api/hosts", tags=["hosts"])


def _refresh_machines_file(db, settings) -> None:
    """Rewrite the generated machines file the agent reads, after anything it mentions changed.

    Called from every write below and never allowed to fail the write: a read-only workspace
    costs the agent a stale sentence, and should not cost the user a 500.
    """
    hosts_view.write_registry_file(
        db,
        settings.resolved_registry_file,
        stale_after_seconds=settings.stale_after_seconds,
    )


def _publish(db, broker, host: HostOut) -> None:
    """Push one host to the live stream.

    Anchored to the newest event id, because a host has no row of its own. See
    `Broker.publish_host` for why that is preferred over a second id space.
    """
    broker.publish_host(host, after=ss.latest_event_id(db))


@router.post(
    "",
    response_model=HostOut,
    dependencies=[Depends(token_header)],
    summary="Report that a connector is here, or that it is leaving",
)
def report_host(
    data: HostReport,
    db: deps.DbDep,
    broker: deps.BrokerDep,
    settings: deps.SettingsDep,
) -> HostOut:
    host = hs.report_host(db, data)
    _refresh_machines_file(db, settings)
    _publish(db, broker, host)
    return host


@router.get(
    "",
    response_model=HostListOut,
    dependencies=[Depends(dashboard_header)],
    summary="Connected agent hosts, most recently seen first",
)
def list_hosts(
    db: deps.DbDep,
    settings: deps.SettingsDep,
) -> HostListOut:
    hosts = hs.list_hosts(db, stale_after_seconds=settings.stale_after_seconds)
    return HostListOut(hosts=hosts)


# --------------------------------------------------------------------------- #
# the connector's half of access — what *it* has to do about the key
# --------------------------------------------------------------------------- #
@router.get(
    "/access",
    response_model=HostAccessFetchOut,
    dependencies=[Depends(token_header)],
    summary="Ask whether this connector's host has been authorized, and with which key",
)
def fetch_access(
    db: deps.DbDep,
    label: str = Query(max_length=64),
    agent_type: str = Query(max_length=64),
) -> HostAccessFetchOut:
    """Named by `(label, agent_type)` rather than by host id: a connector knows who it is, and
    an id would have to be kept somewhere on the host for no gain. A connector can only ever
    learn a *public* key this way — the private half is not in any response on any route.
    """
    return ha.connector_payload(db, label.strip(), agent_type.strip())


@router.post(
    "/access/installed",
    response_model=HostAccessFetchOut,
    dependencies=[Depends(token_header)],
    summary="Report that the key is now in this machine's authorized_keys (or why it is not)",
)
def report_installed(
    data: HostAccessReport,
    db: deps.DbDep,
    broker: deps.BrokerDep,
    settings: deps.SettingsDep,
) -> HostAccessFetchOut:
    result = ha.mark_installed(
        db, data.label.strip(), data.agent_type.strip(),
        installed=data.installed, error=data.error,
    )
    host_row = db.query_one(
        "SELECT * FROM agent_hosts WHERE label = ? AND agent_type = ?",
        (data.label.strip(), data.agent_type.strip()),
    )
    if host_row is not None:
        _refresh_machines_file(db, settings)
        host = hs.host_for(
            db, host_row["id"], stale_after_seconds=settings.stale_after_seconds
        )
        if host is not None:
            _publish(db, broker, host)
    return result


# --------------------------------------------------------------------------- #
# the person's half of access — approving one machine
# --------------------------------------------------------------------------- #
@router.post(
    "/{host_id}/access",
    response_model=HostAccessOut,
    dependencies=[Depends(access_header)],
    summary="Authorize the personal agent to ssh into this host",
)
def authorize_access(
    host_id: str,
    data: HostAccessRequest,
    db: deps.DbDep,
    broker: deps.BrokerDep,
    settings: deps.SettingsDep,
) -> HostAccessOut:
    """Approve, or re-approve after the host moved.

    `access_header`, not `dashboard_header`: this hands out a way in, so it is an action, and
    an action is refused outright when no access token is configured rather than being left
    open on a deployment that never set one.
    """
    host_row = hs.get_host_row(db, host_id)
    if host_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown host")
    try:
        result = ha.authorize(
            db,
            host_row,
            key_dir=settings.resolved_agent_key_dir,
            ssh_host=data.ssh_host,
            ssh_user=data.ssh_user,
            ssh_port=data.ssh_port,
        )
    except ha.AccessError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    _refresh_machines_file(db, settings)
    _publish(db, broker, result.host)
    return result


@router.delete(
    "/{host_id}/access",
    response_model=HostOut,
    dependencies=[Depends(access_header)],
    summary="Withdraw access to this host and delete the key",
)
def revoke_access(
    host_id: str,
    db: deps.DbDep,
    broker: deps.BrokerDep,
    settings: deps.SettingsDep,
) -> HostOut:
    host_row = hs.get_host_row(db, host_id)
    if host_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown host")
    try:
        host = ha.revoke(db, host_row)
    except ha.AccessError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    _refresh_machines_file(db, settings)
    _publish(db, broker, host)
    return host


__all__ = ["router"]
