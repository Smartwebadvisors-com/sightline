"""Flask app for the internal Sightline dashboard.

Runs on 127.0.0.1 only. No auth here — Cloudflare Access sits in front.
No CSRF — internal single-tenant tool. Do not expose publicly."""
from __future__ import annotations

import logging

from flask import (Flask, Response, abort, redirect, render_template,
                   request, url_for)

from .. import db
from ..pipeline import create_pending_scan
from ..render.html import render_scan
from ..scoring import apply as apply_mod
from ..scoring import weights as weights_mod
from . import runner

log = logging.getLogger(__name__)


def _current_wv_id() -> int:
    """Return the id of the current weights version, creating the row if
    weights.py has been bumped and init-db hasn't been re-run."""
    wv = db.get_weight_version(weights_mod.WEIGHTS_VERSION)
    if wv:
        return wv["id"]
    return db.upsert_weight_version(
        weights_mod.WEIGHTS_VERSION,
        weights_mod.WEIGHTS_DESCRIPTION,
        weights_mod.snapshot(),
    )


def _grouped():
    return db.list_scans_grouped(_current_wv_id())


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template(
            "list.html",
            grouped=_grouped(),
            error=None,
            prefill="",
            weights_version=weights_mod.WEIGHTS_VERSION,
        )

    @app.post("/scans")
    def new_scan():
        raw = (request.form.get("url") or "").strip()
        try:
            scan_id = create_pending_scan(raw)
        except ValueError as e:
            return render_template(
                "list.html",
                grouped=_grouped(),
                error=str(e),
                prefill=raw,
                weights_version=weights_mod.WEIGHTS_VERSION,
            ), 400
        scan = db.scan(scan_id)
        runner.submit(scan_id, scan["url"])
        return redirect(url_for("report", scan_id=scan_id))

    @app.get("/report/<int:scan_id>")
    def report(scan_id: int):
        scan = db.scan(scan_id)
        if not scan:
            abort(404)
        if scan["status"] in ("running", "queued"):
            return render_template("pending.html", scan=scan), 200
        if scan["status"] == "failed":
            return render_template("failed.html", scan=scan), 200
        wv_id = _current_wv_id()
        # Ensure the scan has scores under the current weights version.
        # apply_to_scan is idempotent (ON CONFLICT DO UPDATE), so this is
        # safe to call on every render and correct across weight bumps.
        apply_mod.apply_to_scan(scan_id, weights_mod.snapshot(), wv_id)
        html = render_scan(scan_id, wv_id)
        return Response(html, mimetype="text/html; charset=utf-8")

    @app.post("/scans/<int:scan_id>/delete")
    def delete(scan_id: int):
        db.delete_scan(scan_id)
        return redirect(url_for("index"))

    return app
