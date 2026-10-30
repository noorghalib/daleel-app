"""Bounded synchronous inference. Product users, storage and jobs live upstream."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from datetime import date
from pathlib import Path
from threading import Lock
from time import perf_counter
from uuid import uuid4

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException, SecurityError
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
API_VERSION = "1.0.0"
SECTORS = ["energy", "healthcare", "luxurygoods", "manufacturing", "transport"]
LIMITS = {"body_bytes": 10 * 1024 * 1024, "transactions": 10000,
          "headers": 1000, "lines": 10000, "chart_of_accounts": 10000}
TX = ["record_id", "step", "type", "amount", "oldbalanceOrg", "newbalanceOrig",
      "oldbalanceDest", "newbalanceDest"]
HEADERS = ["document_id", "posting_date", "document_date", "document_type", "source", "business_process"]
LINES = ["document_id", "line_number", "gl_account", "debit_amount", "credit_amount", "local_amount", "auxiliary_account_number"]
ACCOUNTS = ["account_number", "account_type"]
ACCOUNT_TYPES = ["Asset", "Liability", "Equity", "Tiers", "Expense", "Revenue", "Special"]


class InputError(Exception):
    def __init__(self, message, code="invalid_input", status=422):
        super().__init__(message)
        self.code, self.status = code, status


def keys(obj, required, optional=()):
    if not isinstance(obj, dict):
        raise InputError("Expected a JSON object")
    missing = set(required) - obj.keys()
    extra = obj.keys() - set(required) - set(optional)
    if missing or extra:
        raise InputError(f"Missing fields: {', '.join(sorted(missing)) or '-'}; "
                         f"unsupported fields: {', '.join(sorted(extra)) or '-'}")


def rows(payload, name, fields, allow_empty=False):
    value = payload[name]
    if not isinstance(value, list):
        raise InputError(f"{name} must be an array")
    if not allow_empty and not value:
        raise InputError(f"{name} must not be empty")
    if len(value) > LIMITS[name]:
        raise InputError(f"{name} exceeds {LIMITS[name]} rows", "batch_too_large", 413)
    for i, row in enumerate(value):
        try:
            keys(row, fields)
        except InputError as exc:
            raise InputError(f"{name}[{i}]: {exc}") from exc
    return value


def string(value, field, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise InputError(f"{field} must be a nonempty string of at most 256 characters")


def number(value, field, nullable=True, integer=False):
    if value is None and nullable:
        return
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or abs(value) > 1e15 or not math.isfinite(value)):
        raise InputError(f"{field} must be a finite JSON number with abs(value) <= 1e15" +
                         (" or null" if nullable else ""))
    if integer and (value < 0 or int(value) != value):
        raise InputError(f"{field} must be a nonnegative integer")


def unique(values, field):
    if len(values) != len(set(values)):
        raise InputError(f"{field} must be unique within the request")


def load_bundle():
    # Validate bundled bytes before joblib deserialization. No user-supplied paths.
    import daleel_ml
    artifacts = Path(daleel_ml.__file__).parent / "artifacts"
    identity = json.loads((ROOT / "MODEL_IDENTITY.json").read_text(encoding="utf-8"))
    for name, expected in identity["artifacts"].items():
        actual = hashlib.sha256((artifacts / name).read_bytes()).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise RuntimeError(f"Bundled model integrity check failed: {name}")
    return daleel_ml.load_model(), daleel_ml.load_ledger_model(), identity


def create_app(*, api_token=None, bundle=None):
    token = api_token if api_token is not None else os.environ.get("DALEEL_API_TOKEN", "")
    if len(token) < 32 or not token.isascii() or not token.isprintable() or " " in token:
        raise RuntimeError("Set DALEEL_API_TOKEN to a private ASCII token of at least 32 characters")
    tx_model, ledger_model, identity = bundle if bundle is not None else load_bundle()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = LIMITS["body_bytes"]
    app.json.ensure_ascii = False
    inference_lock = Lock()

    @app.before_request
    def authenticate():
        g.request_id = str(uuid4())
        if isinstance(request.routing_exception, SecurityError):
            raise request.routing_exception
        if (request.path == "/health" or request.endpoint in app.config.get("DALEEL_PUBLIC_ENDPOINTS", set())) and request.method in {"GET", "HEAD"}:
            return None
        browser_auth = app.extensions.get("daleel_browser_auth")
        if browser_auth and browser_auth():
            return None
        supplied = request.headers.get("Authorization", "").encode("utf-8")
        expected = ("Bearer " + token).encode("utf-8")
        if not hmac.compare_digest(supplied, expected):
            raise InputError("A valid service Bearer token is required", "unauthorized", 401)

    @app.after_request
    def response_headers(response):
        response.headers["X-Request-ID"] = getattr(g, "request_id", "")
        response.headers["Cache-Control"] = "no-store"
        if response.status_code == 401:
            response.headers["WWW-Authenticate"] = "Bearer"
        if response.status_code == 429:
            response.headers["Retry-After"] = "1"
        return response

    def error(message, code, status):
        if not getattr(g, "request_id", None):
            g.request_id = str(uuid4())
        return jsonify({"request_id": g.request_id, "error": {"code": code, "message": message}}), status

    @app.errorhandler(InputError)
    def input_error(exc):
        return error(str(exc), exc.code, exc.status)

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return error(exc.description, f"http_{exc.code}", exc.code)

    @app.errorhandler(Exception)
    def internal_error(exc):
        # Do not log financial payloads or exception text containing input values.
        app.logger.error("request_id=%s failure_type=%s", g.request_id, type(exc).__name__)
        return error("Inference failed; contact the service operator with request_id", "internal_error", 500)

    def payload():
        if not request.is_json:
            raise InputError("Content-Type must be application/json", "unsupported_media_type", 415)
        value = request.get_json()
        if not isinstance(value, dict):
            raise InputError("Request body must be a JSON object")
        return value

    def infer(fn, kind, policy=None):
        # A process runs one prediction at a time; the models limit native threads to 8.
        if not inference_lock.acquire(blocking=False):
            raise InputError("Model is busy; retry with backoff", "busy", 429)
        started = perf_counter()
        try:
            try:
                result = fn()
            except ValueError as exc:
                raise InputError(str(exc)) from exc
            # pandas serialization converts missing scalars and numpy types to JSON.
            predictions = json.loads(result.to_json(orient="records", force_ascii=False, double_precision=15))
        finally:
            inference_lock.release()
        response = {"request_id": g.request_id, "api_version": API_VERSION,
                    "model_id": identity["model_ids"][kind], "count": len(predictions),
                    "elapsed_ms": round((perf_counter() - started) * 1000, 3), "predictions": predictions}
        if policy is not None:
            response["review_policy"] = policy
        return jsonify(response)

    @app.get("/health")
    def health():
        return jsonify({"status": "ready", "api_version": API_VERSION})

    @app.get("/v1/models")
    def models():
        return jsonify({"api_version": API_VERSION, "identity": identity, "limits": LIMITS,
                        "sectors": SECTORS, "ledger_default_review_fraction": .01})

    @app.post("/v1/transactions/predict")
    def transactions():
        return predict_transactions(payload())

    def predict_transactions(data):
        keys(data, ["transactions"])
        records = rows(data, "transactions", TX)
        for row in records:
            string(row["record_id"], "record_id")
            string(row["type"], "type")
            number(row["step"], "step", nullable=False, integer=True)
            for field in TX[3:]:
                number(row[field], field)
        unique([r["record_id"] for r in records], "record_id")
        return infer(lambda: tx_model.predict(pd.DataFrame(records)), "transactions")

    app.extensions["daleel_predict_transactions"] = predict_transactions

    @app.post("/v1/ledger/predict")
    def ledger():
        return predict_ledger(payload())

    def predict_ledger(data):
        keys(data, ["sector", "headers", "lines", "chart_of_accounts"], ["review_fraction"])
        if not isinstance(data["sector"], str) or data["sector"] not in SECTORS:
            raise InputError("sector must be one of: " + ", ".join(SECTORS))
        fraction = data.get("review_fraction", .01)
        if fraction is not None:
            number(fraction, "review_fraction", nullable=False)
            if not 0 < fraction <= 1:
                raise InputError("review_fraction must be in (0, 1] or null")
        heads = rows(data, "headers", HEADERS)
        lines = rows(data, "lines", LINES, allow_empty=True)
        accounts = rows(data, "chart_of_accounts", ACCOUNTS, allow_empty=True)
        for row in heads:
            for field in HEADERS:
                string(row[field], field, nullable=field == "document_date")
            try:
                parsed = date.fromisoformat(row["posting_date"])
                if parsed.isoformat() != row["posting_date"]:
                    raise ValueError()
            except ValueError:
                raise InputError("posting_date must be a valid YYYY-MM-DD date") from None
        unique([r["document_id"] for r in heads], "headers.document_id")
        head_ids = {r["document_id"] for r in heads}
        for row in lines:
            for field in ("document_id", "gl_account", "auxiliary_account_number"):
                string(row[field], field, nullable=field == "auxiliary_account_number")
            if row["document_id"] not in head_ids:
                raise InputError("Orphan journal lines reference unknown headers")
            number(row["line_number"], "line_number", nullable=False, integer=True)
            for field in ("debit_amount", "credit_amount", "local_amount"):
                number(row[field], field)
        for row in accounts:
            string(row["account_number"], "account_number")
            string(row["account_type"], "account_type")
            if row["account_type"] not in ACCOUNT_TYPES:
                raise InputError("account_type must be one of: " + ", ".join(ACCOUNT_TYPES))
        unique([r["account_number"] for r in accounts], "chart_of_accounts.account_number")
        policy = {"mode": "fixed_validation_threshold" if fraction is None else "batch_top_fraction",
                  "review_fraction": fraction}
        return infer(lambda: ledger_model.predict(pd.DataFrame(heads, columns=HEADERS),
                     pd.DataFrame(lines, columns=LINES), pd.DataFrame(accounts, columns=ACCOUNTS),
                     data["sector"], review_fraction=fraction), "ledger", policy)

    app.extensions["daleel_predict_ledger"] = predict_ledger
    return app
