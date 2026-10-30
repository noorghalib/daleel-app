"""Local browser sessions and projection of stored analyses into the frontend contract."""
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
from threading import Lock
from time import monotonic

import click
from flask import g, jsonify, request, session
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

from service.app import InputError


def register_workspace(app, db, Analysis, LocalUser):
    # Derive a separate cookie-signing key. The service token is never sent to the browser.
    app.secret_key = hmac.new(os.environ["DALEEL_API_TOKEN"].encode(), b"daleel-browser-session-v1", hashlib.sha256).digest()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=os.environ.get("DALEEL_COOKIE_SECURE") == "1",
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=8), SESSION_REFRESH_EACH_REQUEST=False)
    app.config["DALEEL_PUBLIC_ENDPOINTS"].add("auth_session")
    attempts = defaultdict(deque)
    attempts_lock = Lock()
    dummy_hash = generate_password_hash(secrets.token_hex(24))

    def csrf_check():
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            raise InputError("Request origin is not allowed", "csrf_failed", 403)
        expected = session.get("csrf", "")
        supplied = request.headers.get("X-CSRF-Token", "")
        if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
            raise InputError("Refresh the page and sign in again", "csrf_failed", 403)

    def browser_auth():
        if request.endpoint == "auth_login":
            csrf_check()
            return True
        # Explicit API authorization retains the original Bearer-token semantics.
        if request.headers.get("Authorization"):
            return False
        user_id = session.get("user_id")
        if not user_id:
            return False
        user = db.session.get(LocalUser, user_id)
        if not user:
            session.clear()
            return False
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf_check()
        g.local_user = user
        return True

    app.extensions["daleel_browser_auth"] = browser_auth

    @app.get("/auth/session")
    def auth_session():
        session.setdefault("csrf", secrets.token_hex(32))
        user = db.session.get(LocalUser, session["user_id"]) if session.get("user_id") else None
        return jsonify(authenticated=user is not None, username=user.username if user else None,
                       csrf_token=session["csrf"])

    @app.post("/auth/login")
    def auth_login():
        key = request.remote_addr or "local"
        now = monotonic()
        with attempts_lock:
            while attempts[key] and attempts[key][0] < now - 300:
                attempts[key].popleft()
            if len(attempts[key]) >= 10:
                raise InputError("Too many sign-in attempts; wait five minutes", "login_throttled", 429)
            attempts[key].append(now)
        body = request.get_json()
        if not isinstance(body, dict):
            raise InputError("Enter a username and password")
        username, password = body.get("username"), body.get("password")
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 80 or len(password) > 1024:
            raise InputError("Enter a valid username and password")
        user = db.session.execute(db.select(LocalUser).where(LocalUser.username == username.strip())).scalar_one_or_none()
        valid = check_password_hash(user.password_hash if user else dummy_hash, password)
        if not user or not valid:
            raise InputError("Incorrect username or password", "invalid_credentials", 401)
        with attempts_lock:
            attempts.pop(key, None)
        session.clear()
        session.permanent = True
        session.update(user_id=user.id, csrf=secrets.token_hex(32))
        return jsonify(authenticated=True, username=user.username, csrf_token=session["csrf"])

    @app.post("/auth/logout")
    def auth_logout():
        session.clear()
        return jsonify(authenticated=False)

    @app.cli.command("create-user")
    @click.option("--username", prompt=True, default="admin")
    @click.password_option(confirmation_prompt=True)
    def create_user(username, password):
        """Create a local workspace account. Password is hashed, never stored as plaintext."""
        username = username.strip()
        if not 1 <= len(username) <= 80 or not 12 <= len(password) <= 1024:
            raise click.ClickException("Use a username of 1–80 characters and password of 12–1024 characters.")
        db.create_all()
        if db.session.execute(db.select(LocalUser).where(LocalUser.username == username)).scalar_one_or_none():
            raise click.ClickException("Username already exists; nothing changed.")
        db.session.add(LocalUser(username=username, password_hash=generate_password_hash(password)))
        db.session.commit()
        click.echo("Local account created. Sign in through the website.")

    @app.get("/workspace")
    def workspace():
        try:
            days = int(request.args.get("periodDays", "30"))
        except ValueError:
            raise InputError("periodDays must be 7, 30 or 90") from None
        if days not in {7, 30, 90}:
            raise InputError("periodDays must be 7, 30 or 90")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        data = {"summary": {"total": 0, "normal": 0, "review": 0, "high": 0},
                "transactions": [], "ledger": [], "files": [], "activity": [],
                "notifications": [{"title": "مساحة عمل محلية", "message": "الفترة حسب وقت التحليل. جميع الحسابات المحلية تشترك في البيانات؛ صلاحيات الشركات لم تُضف بعد. الحقول غير المحفوظة تظهر —."}],
                "reports": None, "profile": None}
        if getattr(g, "local_user", None):
            data["profile"] = {"name": g.local_user.username, "company": "Daleel — مساحة محلية", "email": ""}
        daily = defaultdict(lambda: {"transactions": 0, "alerts": 0})
        truncated = False
        try:
            runs = db.session.execute(db.select(Analysis).where(Analysis.created_at >= cutoff)
                                      .order_by(Analysis.created_at.desc(), Analysis.id.desc())
                                      .execution_options(yield_per=20)).scalars()
            for run in runs:
                created = run.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                source = run.result.get("_source", {})
                for index, file in enumerate(source.get("files", [])):
                    if len(data["files"]) < 300:
                        data["files"].append({"id": f"{run.id}:{index}", "name": file["name"], "size": file["size"],
                                              "kind": run.kind, "status": "completed", "createdAt": created.isoformat()})
                    else:
                        truncated = True
                for index, prediction in enumerate(run.result["predictions"]):
                    key = prediction.get("record_id", prediction.get("document_id"))
                    details = source.get("records", {}).get(key, {})
                    model_alert = prediction.get("prediction") == "Anomaly" if run.kind == "transactions" else bool(prediction.get("review_alert"))
                    rule_alert = bool(prediction.get("rule_alert"))
                    status = "review" if model_alert or rule_alert else "normal"
                    reason = ("النموذج: يحتاج مراجعة. " if model_alert else "النموذج: لا يوجد تنبيه. ")
                    reason += "القواعد: " + (prediction.get("rule_reasons") or ("تنبيه مستقل" if rule_alert else "لا توجد مخالفة"))
                    record = {"id": f"{run.id}:{index}", "name": key, "amount": details.get("amount"),
                              "currency": None, "type": details.get("type"), "date": details.get("date"),
                              "sector": prediction.get("sector"), "status": status,
                              "score": prediction.get("fraud_score", prediction.get("anomaly_score")),
                              "reason": reason, "reviewed": False, "reviewNote": None}
                    if len(data[run.kind]) < 2000:
                        data[run.kind].append(record)
                    else:
                        truncated = True
                    if run.kind == "transactions":
                        data["summary"]["total"] += 1
                        data["summary"][status] += 1
                        daily[created.date().isoformat()]["transactions"] += 1
                        daily[created.date().isoformat()]["alerts"] += int(status == "review")
        except SQLAlchemyError:
            db.session.rollback()
            return jsonify(error={"code": "storage_unavailable", "message": "Database is unavailable"}), 503
        data["activity"] = [{"label": day, **values} for day, values in sorted(daily.items())]
        if truncated:
            data["notifications"].append({"title": "عرض محدود", "message": "الجداول تعرض أحدث 2000 سجل لكل نوع و300 ملف. الملخص يشمل كل المعاملات في الفترة؛ البحث والتصدير يشملان السجلات المحملة فقط."})
        identity = json.loads((Path(__file__).parent / "MODEL_IDENTITY.json").read_text(encoding="utf-8"))
        data["models"] = [
            {"id": identity["model_ids"]["transactions"], "name": "نموذج المعاملات", "family": "HistGradientBoosting",
             "version": identity["model_ids"]["transactions"], "status": "متاح", "description": "تصنيف المعاملات؛ الدرجة ليست احتمالاً مضموناً للاحتيال."},
            {"id": identity["model_ids"]["ledger"], "name": "نماذج القيود", "family": "Isolation Forest",
             "version": identity["model_ids"]["ledger"], "status": "متاح", "description": "خمسة قطاعات؛ ترتيب الشذوذ للمراجعة، وليس إثبات الاحتيال."}]
        return jsonify(data)
