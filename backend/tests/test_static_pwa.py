"""The PWA files are part of the served surface, not just build output.

The static mount used to serve `/assets` and `/` only. `manifest.webmanifest`, `sw.js` and
`icons/` live beside the bundle, so the whole directory has to be reachable — and the manifest
needs its own MIME type, because a browser ignores one served as `application/octet-stream`.
This is pinned here rather than discovered in production, where the failure is a silently
uninstallable app.
"""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.main import create_app


def test_pwa_files_are_served_and_do_not_shadow_the_api(settings, db, broker, tmp_path):
    static = tmp_path / "static"
    (static / "icons").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>Sessions</title>", encoding="utf-8")
    (static / "manifest.webmanifest").write_text('{"name": "x"}', encoding="utf-8")
    (static / "sw.js").write_text("/* service worker */", encoding="utf-8")
    (static / "icons" / "icon-192.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    app = create_app(settings=replace(settings, static_dir=static), db=db, broker=broker)
    with TestClient(app) as client:
        manifest = client.get("/manifest.webmanifest")
        assert manifest.status_code == 200
        assert manifest.headers["content-type"].split(";")[0] == "application/manifest+json"

        worker = client.get("/sw.js")
        assert worker.status_code == 200
        assert "javascript" in worker.headers["content-type"]

        icon = client.get("/icons/icon-192.png")
        assert icon.status_code == 200

        # Registration order still wins: the catch-all static mount must not swallow API routes.
        assert client.get("/api/sessions").status_code == 200


def test_the_shell_revalidates_and_hashed_assets_are_immutable(settings, db, broker, tmp_path):
    """A stable file name must not be heuristically cached by a phone's own HTTP cache.

    With no `Cache-Control`, a browser may cache `/` for a heuristic lifetime. An installed
    home-screen app keeps an HTTP cache of its own, so a deploy could reach a fresh browser tab
    and not the app for days — the service worker's network-first navigation cannot help,
    because `fetch()` is allowed to answer from that stale cache. `assets/` is the opposite
    case: Vite puts the build in the file name, so a cached asset can never be stale.
    """
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (static / "sw.js").write_text("/* service worker */", encoding="utf-8")
    (static / "assets" / "index-abc123.js").write_text("// app", encoding="utf-8")

    app = create_app(settings=replace(settings, static_dir=static), db=db, broker=broker)
    with TestClient(app) as client:
        assert client.get("/").headers["cache-control"] == "no-cache"
        assert client.get("/sw.js").headers["cache-control"] == "no-cache"
        assert client.get("/index.html").headers["cache-control"] == "no-cache"
        assert (
            client.get("/assets/index-abc123.js").headers["cache-control"]
            == "public, max-age=31536000, immutable"
        )
