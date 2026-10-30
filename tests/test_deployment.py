"""Deployment configuration checks without a live database."""
import os
import unittest
from unittest.mock import patch

from flask import Flask
from deployment import database_url, configure_app, server_options


class DeploymentTests(unittest.TestCase):
    def test_provider_url_and_ssl(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://user:p%40ss@db:5432/daleel?sslmode=require"}, clear=True):
            url = database_url()
            self.assertEqual(url.drivername, "postgresql+psycopg")
            self.assertEqual(url.password, "p@ss")
            self.assertEqual(url.query["sslmode"], "require")

    def test_local_password_is_not_url_interpolated(self):
        with patch.dict(os.environ, {"DB_PASSWORD": "p@ss/#", "DB_SSLMODE": "verify-full"}, clear=True):
            self.assertEqual(database_url().password, "p@ss/#")
            self.assertEqual(database_url().query["sslmode"], "verify-full")

    def test_rejects_other_database_drivers(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///private.db"}, clear=True):
            with self.assertRaises(RuntimeError):
                database_url()

    def test_hosting_port_and_untrusted_headers_default(self):
        with patch.dict(os.environ, {"DALEEL_ENV": "production", "PORT": "8080"}, clear=True):
            options = server_options()
            self.assertEqual((options["host"], options["port"]), ("0.0.0.0", 8080))
            self.assertTrue(options["clear_untrusted_proxy_headers"])
            self.assertNotIn("trusted_proxy", options)

    def test_proxy_is_explicit(self):
        with patch.dict(os.environ, {"DALEEL_TRUSTED_PROXY": "127.0.0.1"}, clear=True):
            options = server_options()
            self.assertEqual(options["trusted_proxy"], "127.0.0.1")
            self.assertIn("x-forwarded-proto", options["trusted_proxy_headers"])

    def test_production_requires_hosts_and_secure_cookies(self):
        with patch.dict(os.environ, {"DALEEL_ENV": "production"}, clear=True):
            with self.assertRaises(RuntimeError):
                configure_app(Flask(__name__))
        with patch.dict(os.environ, {"DALEEL_ENV": "production", "DALEEL_ALLOWED_HOSTS": "daleel.test", "DALEEL_COOKIE_SECURE": "0"}, clear=True):
            with self.assertRaises(RuntimeError):
                configure_app(Flask(__name__))

    def test_production_headers_and_host_validation(self):
        app = Flask(__name__)
        with patch.dict(os.environ, {"DALEEL_ENV": "production", "DALEEL_ALLOWED_HOSTS": "daleel.test"}, clear=True):
            configure_app(app)
        app.add_url_rule("/", view_func=lambda: "ready")
        client = app.test_client()
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
        response = client.get("/", base_url="https://daleel.test")
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(client.get("/", base_url="https://untrusted.test").status_code, 400)
