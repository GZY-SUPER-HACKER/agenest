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
