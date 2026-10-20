"""Bearer-token authentication.

Two credentials, and the split is not "which page" — it is *this service's own page* versus
*the machines that report to it*:

- **Access token** (`CSD_DASHBOARD_TOKEN`) — one credential for the whole page: reading,
  closing a session, and driving the agent. It is the token in the URL fragment, and it is
  the only one a person ever sees. Optional, but its enforcement is deliberately **not**
  uniform: reads are gated only when it is set, while the routes that *act* refuse to run at
  all without it. `access_header` carries the argument.
- **Ingest token** (`CSD_INGEST_TOKEN`) — required. Grants `POST /api/sessions`, event
  reporting, and `PATCH`. The server refuses to start without it. Kept separate because it is
  not a way into the page: it is what a *host* presents when reporting, so it must be
  issuable to machines that should not be able to drive an agent.

Until 2026-10-19 the page had three credentials — read, control, agent. Collapsing them was
the operator's call: three secrets guarding one page is a login ritual, not a boundary, and
the one deployment that has to satisfy it is a single-operator box behind Tailscale. What the
collapse must not do is make the *acting* routes open, which is why the shared credential
above is still required to exist for them (docs/design.md §22).

Both are read from `request.app.state` rather than passed as parameters. A dependency that
takes a plain dataclass as an argument is indistinguishable to FastAPI from a request-body
field, which is exactly how a security check silently becomes optional (it 422s instead of
401ing). Pulling from app state makes the dependency parameterless, so it has no surface to
mis-parse.

Token comparison uses `secrets.compare_digest`: the value is never rejected early, so a
timing side channel leaks at most the length, never the characters.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

from .config import Settings

INGEST_HEADER = "X-CPD-Token"


def _matches(supplied: str, expected: str) -> bool:
    """Constant-time comparison that survives a non-ASCII candidate.

    `secrets.compare_digest` raises `TypeError` when given non-ASCII `str`, and the query
    parameter it is fed here is exactly the place a mangled value arrives: a pasted link with a
    sentence glued onto the token produced a **500** on the SSE route instead of a 401. A bad
    credential is never a server error, so the comparison happens on UTF-8 bytes — which is what
    the function wants anyway, and still in constant time.
    """
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="missing or invalid token",
    headers={"WWW-Authenticate": "Bearer"},
)


def _extract(header_value: str | None) -> str | None:
    """Accept `Authorization: Bearer <t>` or the bare `X-CPD-Token: <t>` form."""
    if not header_value:
        return None
    value = header_value.strip()
    lowered = value.lower()
    if lowered.startswith("token "):
        value = value[6:].strip()
    elif lowered.startswith("bearer "):
        value = value[7:].strip()
    return value or None


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def token_header(request: Request) -> None:
    """FastAPI dependency for ingest endpoints."""
    settings = _settings(request)
    supplied = _extract(request.headers.get(INGEST_HEADER) or request.headers.get("authorization"))
    if not supplied or not _matches(supplied, settings.ingest_token):
        raise _UNAUTHENTICATED


def dashboard_header(request: Request) -> None:
    """FastAPI dependency for read endpoints. A no-op unless an access token is set.

    Accepts the token from a query parameter as well as a header, and that exception is
    narrow on purpose. `EventSource` — the only client the SSE stream has — cannot set
    request headers, so a browser reaching this over the public internet has no other way to
    authenticate the stream. The cost is real and worth naming: a query string lands in
    browser history, in `Referer` headers, and in every proxy log on the path. It is
    tolerated here because this dependency is on the *read* routes; `access_header` below is
    where the same token stops being acceptable in a URL.

    The ingest token is deliberately **not** accepted from a URL, in either place. It can
    write, and a credential that can write should never be something a server writes into
    its own logs.
    """
    settings = _settings(request)
    if not settings.dashboard_token:
        return
    supplied = _extract(
        request.headers.get(INGEST_HEADER)
        or request.headers.get("authorization")
        or request.query_params.get("token")
    )
    if not supplied or not _matches(supplied, settings.dashboard_token):
        raise _UNAUTHENTICATED


def access_header(request: Request) -> None:
    """FastAPI dependency for the endpoints that *do* something: close a session, drive the agent.

    **The same token as a read — but it has to exist.** One credential is what makes "paste the
    link, it works" true (docs/design.md §22). What is *not* shared is the consequence of its
    absence, and that asymmetry is the whole reason this is a separate dependency rather than
    reusing `dashboard_header`:

    - A missing read token leaves reads open, which costs nothing on a private deployment.
    - A missing token here would leave a session-closer, and an agent with `bash` on this host,
      open to whoever loads the page. A default that did that would be the worst default in
      this codebase, so this raises 403 instead — "acting is not enabled on this server" — and
      401 when a token is configured but the caller's is wrong. Two answers, because they are
      two different problems: one is fixed in `/etc/csd/csd.env`, the other in this browser.

    **Header only, never `?token=`.** The query-parameter concession on the read path exists
    solely because `EventSource` cannot set request headers; nothing here is called by
    `EventSource`, because the agent stream is consumed with `fetch()` and a real header
    (`frontend/src/api/agent.ts`). A URL is written into browser history, `Referer` headers and
    every proxy log on the path, and this credential can run commands.
    """
    settings = _settings(request)
    if not settings.dashboard_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this server has no access token set (set CSD_DASHBOARD_TOKEN)",
        )
    supplied = _extract(
        request.headers.get(INGEST_HEADER) or request.headers.get("authorization")
    )
    if not supplied or not _matches(supplied, settings.dashboard_token):
        raise _UNAUTHENTICATED
