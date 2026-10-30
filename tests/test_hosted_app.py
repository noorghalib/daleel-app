"""Production-mode authentication, migrations, and uploads on isolated storage."""
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from flask_migrate import upgrade, downgrade
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect
from waitress.proxy_headers import proxy_headers_middleware
from werkzeug.security import generate_password_hash

from app import create_app, db, LocalUser

ROOT = Path(__file__).resolve().parents[1]


class HostedAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        original = SQLAlchemy.init_app

        def isolated_init(extension, app):
            app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
            app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {}
            original(extension, app)

        cls.environment = {"DALEEL_ENV": "production", "DALEEL_ALLOWED_HOSTS": "daleel.test",
                           "DALEEL_COOKIE_SECURE": "1", "DB_PASSWORD": "test-only", "DALEEL_API_TOKEN": "x" * 64}
        with patch.dict(os.environ, cls.environment), patch.object(SQLAlchemy, "init_app", isolated_init):
            cls.app = create_app()
        cls.app.wsgi_app = proxy_headers_middleware(cls.app.wsgi_app, trusted_proxy="127.0.0.1",
            trusted_proxy_count=1, trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"})
        cls.app.testing = True

    def setUp(self):
        self.environment_patch = patch.dict(os.environ, self.environment)
        self.environment_patch.start()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        with db.engine.begin() as connection:
            from sqlalchemy import text
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        db.engine.dispose()
        self.context.pop()
        self.environment_patch.stop()

    def test_initial_migration_upgrade_and_downgrade(self):
        db.drop_all()
        upgrade(directory=str(ROOT / "migrations"))
        self.assertTrue({"daleel_local_users", "daleel_analyses"}.issubset(inspect(db.engine).get_table_names()))
        downgrade(directory=str(ROOT / "migrations"), revision="base")
        self.assertNotIn("daleel_analyses", inspect(db.engine).get_table_names())

    def test_host_and_private_files(self):
        self.assertEqual(self.client.get("/", base_url="https://evil.test").status_code, 400)
        response = self.client.get("/", base_url="https://daleel.test")
        self.assertEqual(response.status_code, 200)
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(self.client.get("/upload", base_url="https://daleel.test").status_code, 404)
        for path in ("/.env", "/README.md", "/ml-engine/daleel_ml/artifacts/ledger_detector.joblib"):
            self.assertNotEqual(self.client.get(path, base_url="https://daleel.test").status_code, 200)

    def test_https_proxy_login_uploads_and_logout(self):
        db.session.add(LocalUser(username="reviewer", password_hash=generate_password_hash("test-password-only")))
        db.session.commit()
        headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "daleel.test", "X-Forwarded-For": "192.0.2.20"}
        options = {"base_url": "https://daleel.test", "headers": headers, "environ_base": {"REMOTE_ADDR": "127.0.0.1"}}
        preflight = self.client.get("/auth/session", **options)
        self.assertIn("Secure", preflight.headers["Set-Cookie"])
        self.assertIn("HttpOnly", preflight.headers["Set-Cookie"])
        headers.update({"Origin": "https://daleel.test", "X-CSRF-Token": preflight.json["csrf_token"]})
        login = self.client.post("/auth/login", json={"username": "reviewer", "password": "test-password-only"}, **options)
        self.assertEqual(login.status_code, 200, login.text)
        headers["X-CSRF-Token"] = login.json["csrf_token"]
        root = ROOT / "ml-engine/examples/input/demo-scenarios"
        response = self.client.post("/analyses/transactions/upload", data={"file": (io.BytesIO((root / "transactions_demo.csv").read_bytes()), "transactions.csv")}, **options)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json["count"], 20)
        files = {key: (io.BytesIO((root / name).read_bytes()), name) for key, name in
                 (("headers", "ledger_headers.csv"), ("lines", "ledger_lines.csv"), ("chart_of_accounts", "ledger_accounts.csv"))}
        response = self.client.post("/analyses/ledger/upload", data={**files, "sector": "healthcare"}, **options)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json["count"], 12)
        workspace = self.client.get("/workspace", **options).json
        self.assertEqual(workspace["summary"]["review"], 15)
        self.assertEqual(sum(row["status"] == "review" for row in workspace["ledger"]), 9)
        headers["Origin"] = "https://evil.test"
        self.assertEqual(self.client.post("/auth/logout", **options).status_code, 403)
        headers["Origin"] = "https://daleel.test"
        self.assertEqual(self.client.post("/auth/logout", **options).status_code, 200)
        self.assertEqual(self.client.get("/workspace", **options).status_code, 401)
