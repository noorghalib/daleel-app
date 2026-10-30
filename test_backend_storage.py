"""Storage contract tests using bundled ML models and an isolated SQLite database."""
import json
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import SQLAlchemyError

from app import create_app, db


class StorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        original = SQLAlchemy.init_app

        def isolated_init(extension, app):
            app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
            app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {}
            original(extension, app)

        with patch.dict(os.environ, {"DB_PASSWORD": "test-only", "DALEEL_API_TOKEN": "x" * 64}):
            with patch.object(SQLAlchemy, "init_app", isolated_init):
                cls.app = create_app()
        cls.app.testing = True
        cls.auth = {"Authorization": "Bearer " + "x" * 64}
        cls.root = Path(__file__).parent

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.context.pop()

    def test_real_predictions_saved_and_retrieved(self):
        for kind in ("transactions", "ledger"):
            payload = json.loads((self.root / "examples" / f"{kind}.request.json").read_text())
            response = self.client.post(f"/analyses/{kind}", json=payload, headers=self.auth)
            self.assertEqual(response.status_code, 201, response.text)
            saved = self.client.get("/analyses/" + response.json["analysis_id"], headers=self.auth)
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json["predictions"], response.json["predictions"])
            self.assertEqual(saved.json["model_id"], response.json["model_id"])
        listing = self.client.get("/analyses", headers=self.auth)
        self.assertEqual(len(listing.json["analyses"]), 2)
        self.assertFalse(listing.json["has_more"])

    def test_auth_validation_and_not_found(self):
        self.assertEqual(self.client.get("/analyses").status_code, 401)
        self.assertEqual(self.client.post("/analyses/transactions", json={}).status_code, 401)
        self.assertEqual(self.client.post("/analyses/transactions", json={}, headers=self.auth).status_code, 422)
        self.assertEqual(self.client.get("/analyses?page=0", headers=self.auth).status_code, 422)
        self.assertEqual(self.client.get("/analyses/00000000-0000-0000-0000-000000000000", headers=self.auth).status_code, 404)
        self.assertEqual(self.client.get("/analyses", headers=self.auth).json["analyses"], [])

    def test_commit_failure_is_not_reported_as_success(self):
        payload = json.loads((self.root / "examples/transactions.request.json").read_text())
        with patch.object(db.session, "commit", side_effect=SQLAlchemyError("private detail")):
            response = self.client.post("/analyses/transactions", json=payload, headers=self.auth)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private detail", response.text)
        self.assertEqual(self.client.get("/analyses", headers=self.auth).json["analyses"], [])

    def upload(self, content, auth=True):
        return self.client.post("/analyses/transactions/upload", data={
            "file": (io.BytesIO(content), "transactions.csv")
        }, headers=self.auth if auth else {})

    def test_upload_page(self):
        with self.client.get("/upload") as page:
            self.assertEqual(page.status_code, 200)
            self.assertIn(b'ledger-form', page.data)

    def test_frontend_files_public_but_backend_remains_protected(self):
        for path in ("/", "/styles.css", "/app.js", "/workspace.js", "/integration.js",
                     "/assets/favicon.svg", "/assets/IBMPlexSansArabic-Regular.ttf"):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200, path)
        self.assertEqual(self.client.get("/analyses").status_code, 401)
        self.assertEqual(self.client.get("/health/db").status_code, 401)
        self.assertEqual(self.client.get("/v1/models").status_code, 401)
        self.assertEqual(self.client.get("/assets/../../.env").status_code, 404)
        self.assertEqual(self.client.get("/.env").status_code, 401)

    def test_csv_roundtrip(self):
        content = (self.root / "ml-engine/examples/input/transactions.csv").read_bytes()
        self.assertEqual(self.upload(content, auth=False).status_code, 401)
        response = self.upload(content)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json["count"], 3)
        saved = self.client.get("/analyses/" + response.json["analysis_id"], headers=self.auth)
        self.assertEqual(saved.json["predictions"], response.json["predictions"])

    def test_csv_errors_do_not_create_records(self):
        header = b"step,type,amount,oldbalanceOrg,newbalanceOrig,oldbalanceDest,newbalanceDest\n"
        for content in (b"", b"wrong\n1\n", header,
                        header + b"10,PAYMENT,oops,500,400,0,0\n",
                        header + b"10,PAYMENT,NaN,500,400,0,0\n",
                        header + b"10,PAYMENT,100,500,400,0\n",
                        header + b"10.5,PAYMENT,100,500,400,0,0\n",
                        header + b"\n", b"\xff\xfe"):
            with self.subTest(content=content):
                self.assertEqual(self.upload(content).status_code, 422)
        self.assertEqual(self.client.get("/analyses", headers=self.auth).json["analyses"], [])

    def test_csv_generates_ids_and_preserves_missing_amounts(self):
        content = (b"step,type,amount,oldbalanceOrg,newbalanceOrig,oldbalanceDest,newbalanceDest\n"
                   b"10,PAYMENT,,500,400,0,0\n")
        response = self.upload(content)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json["predictions"][0]["record_id"], "row-1")

    def test_upload_limits_and_duplicate_ids(self):
        from csv_upload import read_transactions
        from werkzeug.datastructures import FileStorage
        from service.app import InputError
        content = (self.root / "ml-engine/examples/input/transactions.csv").read_bytes()
        with patch.dict("csv_upload.LIMITS", {"transactions": 1}):
            with self.assertRaises(InputError) as caught:
                read_transactions(FileStorage(io.BytesIO(content), filename="input.csv"))
            self.assertEqual(caught.exception.status, 413)
        lines = content.splitlines()
        duplicate = b"\n".join([lines[0], lines[1], lines[1]])
        self.assertEqual(self.upload(duplicate).status_code, 422)

    def ledger_files(self):
        return {key: (io.BytesIO((self.root / "ml-engine/examples/input" / name).read_bytes()), name)
                for key, name in [("headers", "ledger_headers.csv"), ("lines", "ledger_lines.csv"),
                                  ("chart_of_accounts", "ledger_accounts.csv")]}

    def test_ledger_csv_matches_json_and_saves(self):
        example = json.loads((self.root / "examples/ledger.request.json").read_text())
        for sector in ("energy", "healthcare", "luxurygoods", "manufacturing", "transport"):
            data = self.ledger_files()
            data["sector"] = sector
            response = self.client.post("/analyses/ledger/upload", data=data, headers=self.auth)
            self.assertEqual(response.status_code, 201, response.text)
            example["sector"] = sector
            direct = self.client.post("/v1/ledger/predict", json=example, headers=self.auth)
            self.assertEqual(response.json["predictions"], direct.json["predictions"])
            self.assertEqual(response.json["review_policy"]["review_fraction"], .01)
            saved = self.client.get("/analyses/" + response.json["analysis_id"], headers=self.auth)
            self.assertEqual(saved.json["kind"], "ledger")
            self.assertEqual(saved.json["predictions"], response.json["predictions"])

    def test_ledger_csv_invalid_requests(self):
        self.assertEqual(self.client.post("/analyses/ledger/upload").status_code, 401)
        self.assertEqual(self.client.post("/analyses/ledger/upload", data={"sector": "energy"}, headers=self.auth).status_code, 422)
        for sector in (None, "unsupported"):
            data = self.ledger_files()
            if sector is not None:
                data["sector"] = sector
            self.assertEqual(self.client.post("/analyses/ledger/upload", data=data, headers=self.auth).status_code, 422)
        data = self.ledger_files()
        data["sector"] = "energy"
        lines = data["lines"][0].getvalue().replace(b"demo-001", b"unknown")
        data["lines"] = (io.BytesIO(lines), "lines.csv")
        self.assertEqual(self.client.post("/analyses/ledger/upload", data=data, headers=self.auth).status_code, 422)
        self.assertEqual(self.client.get("/analyses", headers=self.auth).json["analyses"], [])

    def test_ledger_csv_parser_identifiers_nulls_and_limits(self):
        from csv_upload import read_ledger_table
        from werkzeug.datastructures import FileStorage
        from service.app import InputError, LINES
        content = (','.join(LINES) + '\n0001,1,00100,,0,0,\n').encode()
        def parse(value=content):
            return read_ledger_table(FileStorage(io.BytesIO(value), filename="lines.csv"), "lines")
        row = parse()[0]
        self.assertEqual(row["document_id"], "0001")
        self.assertEqual(row["gl_account"], "00100")
        self.assertIsNone(row["debit_amount"])
        self.assertIsNone(row["auxiliary_account_number"])
        self.assertEqual(parse((','.join(LINES) + '\n').encode()), [])
        for bad in (content.replace(b",1,", b",1.5,"), content.replace(b",,0,", b",NaN,0,"), b"wrong\n1\n", b"\xff"):
            with self.assertRaises(InputError):
                parse(bad)
        with patch.dict("csv_upload.LIMITS", {"lines": 0}):
            with self.assertRaises(InputError) as caught:
                parse()
            self.assertEqual(caught.exception.status, 413)

    def create_local_user(self):
        from app import LocalUser
        from werkzeug.security import generate_password_hash
        db.session.add(LocalUser(username="tester", password_hash=generate_password_hash("local-test-password")))
        db.session.commit()

    def sign_in(self):
        self.create_local_user()
        csrf = self.client.get("/auth/session").json["csrf_token"]
        response = self.client.post("/auth/login", json={"username": "tester", "password": "local-test-password"},
                                    headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json["csrf_token"]

    def test_browser_session_csrf_and_logout(self):
        self.assertEqual(self.client.get("/workspace").status_code, 401)
        self.assertEqual(self.client.post("/auth/login", json={}).status_code, 403)
        csrf = self.sign_in()
        self.assertEqual(self.client.get("/workspace").status_code, 200)
        self.assertEqual(self.client.get("/auth/session").json["username"], "tester")
        payload = json.loads((self.root / "examples/transactions.request.json").read_text())
        self.assertEqual(self.client.post("/analyses/transactions", json=payload).status_code, 403)
        self.assertEqual(self.client.post("/analyses/transactions", json=payload,
            headers={"X-CSRF-Token": csrf, "Origin": "https://untrusted.example"}).status_code, 403)
        self.assertEqual(self.client.post("/analyses/transactions", json=payload,
            headers={"X-CSRF-Token": csrf}).status_code, 201)
        self.assertEqual(self.client.post("/auth/logout", headers={"X-CSRF-Token": csrf}).status_code, 200)
        self.assertEqual(self.client.get("/workspace").status_code, 401)

    def test_bad_password_and_session_token_rotation(self):
        self.create_local_user()
        csrf = self.client.get("/auth/session").json["csrf_token"]
        response = self.client.post("/auth/login", json={"username": "tester", "password": "wrong"}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 401)
        response = self.client.post("/auth/login", json={"username": "tester", "password": "local-test-password"}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json["csrf_token"], csrf)
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])

    def test_workspace_existing_and_new_uploads(self):
        content = (self.root / "ml-engine/examples/input/transactions.csv").read_bytes()
        first = self.upload(content)
        second = self.upload(content)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        data = self.ledger_files(); data["sector"] = "energy"
        self.assertEqual(self.client.post("/analyses/ledger/upload", data=data, headers=self.auth).status_code, 201)
        workspace = self.client.get("/workspace?periodDays=30", headers=self.auth)
        self.assertEqual(workspace.status_code, 200, workspace.text)
        body = workspace.json
        self.assertEqual(body["summary"], {"total": 6, "normal": 4, "review": 2, "high": 0})
        self.assertEqual(len(body["transactions"]), 6)
        self.assertEqual(len({r["id"] for r in body["transactions"]}), 6)
        self.assertEqual(len(body["ledger"]), 3)
        self.assertEqual(len(body["files"]), 5)
        self.assertTrue(any(r["amount"] == 100 for r in body["transactions"]))
        self.assertTrue(all(r["currency"] is None for r in body["transactions"]))
        self.assertEqual(len(body["models"]), 2)
        self.assertIsNone(body["reports"])
        self.assertEqual(self.client.get("/workspace?periodDays=5", headers=self.auth).status_code, 422)

    def test_workspace_period_and_missing_legacy_fields(self):
        from app import Analysis
        from datetime import datetime, timedelta, timezone
        payload = json.loads((self.root / "examples/transactions.request.json").read_text())
        response = self.client.post("/analyses/transactions", json=payload, headers=self.auth)
        body = self.client.get("/workspace", headers=self.auth).json
        self.assertIsNone(body["transactions"][0]["amount"])
        self.assertIsNone(body["transactions"][0]["date"])
        self.assertEqual(body["files"], [])
        row = db.session.get(Analysis, response.json["analysis_id"])
        row.created_at = datetime.now(timezone.utc) - timedelta(days=45)
        db.session.commit()
        self.assertEqual(self.client.get("/workspace?periodDays=30", headers=self.auth).json["summary"]["total"], 0)
        self.assertEqual(self.client.get("/workspace?periodDays=90", headers=self.auth).json["summary"]["total"], 3)


if __name__ == "__main__":
    unittest.main()
