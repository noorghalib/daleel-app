"""Contract tests using bundled models; never train or download datasets."""
import copy
import json
import math
from pathlib import Path
from threading import Event, Thread
import unittest

import pandas as pd
from service.app import create_app, load_bundle, LIMITS, TX, HEADERS, LINES, ACCOUNTS, SECTORS

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-only-not-a-real-secret-" + "x" * 40
AUTH = {"Authorization": "Bearer " + TOKEN}


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()
        cls.app = create_app(api_token=TOKEN, bundle=cls.bundle)
        cls.app.testing = True
        cls.tx = json.loads((ROOT / "examples/transactions.request.json").read_text())
        cls.ledger = json.loads((ROOT / "examples/ledger.request.json").read_text())

    def setUp(self):
        self.client = self.app.test_client()

    def post(self, kind, data):
        return self.client.post(f"/v1/{kind}/predict", json=data, headers=AUTH)

    def assert_error(self, response, status):
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(set(body), {"request_id", "error"})
        self.assertEqual(body["request_id"], response.headers["X-Request-ID"])
        self.assertIn("code", body["error"])

    def test_health_and_model_identity(self):
        self.assertEqual(self.client.get("/health").get_json()["status"], "ready")
        response = self.client.get("/v1/models", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["identity"], self.bundle[2])
        self.assertEqual(response.get_json()["limits"], LIMITS)

    def test_requires_token(self):
        self.assert_error(self.client.get("/v1/models"), 401)
        self.assert_error(self.client.get("/v1/models", headers={"Authorization": "Bearer خاطئ"}), 401)

    def test_startup_rejects_weak_token(self):
        for token in ("", "short", "x" * 32 + " ", "س" * 40):
            with self.subTest(token_length=len(token)), self.assertRaises(RuntimeError):
                create_app(api_token=token, bundle=self.bundle)

    def test_bad_json(self):
        response = self.client.post("/v1/transactions/predict", data="{broken", content_type="application/json", headers=AUTH)
        self.assert_error(response, 400)

    def test_content_type(self):
        self.assert_error(self.client.post("/v1/transactions/predict", data="{}", headers=AUTH), 415)

    def test_body_object_required(self):
        self.assert_error(self.post("transactions", []), 422)

    def test_byte_limit(self):
        previous = self.app.config["MAX_CONTENT_LENGTH"]
        try:
            self.app.config["MAX_CONTENT_LENGTH"] = 10
            self.assert_error(self.post("transactions", self.tx), 413)
        finally:
            self.app.config["MAX_CONTENT_LENGTH"] = previous

    def test_unknown_route_and_method(self):
        self.assert_error(self.client.get("/missing", headers=AUTH), 404)
        self.assert_error(self.client.get("/v1/transactions/predict", headers=AUTH), 405)

    def test_transactions_equal_direct_library(self):
        response = self.post("transactions", self.tx)
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        expected = json.loads(self.bundle[0].predict(pd.DataFrame(self.tx["transactions"])).to_json(orient="records", double_precision=15))
        self.assertEqual(result["predictions"], expected)
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["model_id"], self.bundle[2]["model_ids"]["transactions"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_transactions_repeatability_and_new_type(self):
        self.assertEqual(self.post("transactions", self.tx).get_json()["predictions"],
                         self.post("transactions", self.tx).get_json()["predictions"])
        # The third sample uses NEW_TRANSACTION_TYPE and remains in output.
        self.assertEqual(self.post("transactions", self.tx).get_json()["count"], 3)

    def test_missing_field_rejected_per_record(self):
        data = copy.deepcopy(self.tx)
        del data["transactions"][1]["amount"]
        self.assert_error(self.post("transactions", data), 422)

    def test_labels_ids_and_extra_fields_rejected(self):
        for field in ("isFraud", "isFlaggedFraud", "nameOrig", "nameDest", "unexpected"):
            data = copy.deepcopy(self.tx)
            data["transactions"][0][field] = "never used"
            with self.subTest(field=field):
                self.assert_error(self.post("transactions", data), 422)

    def test_empty_and_oversize_transaction_batch(self):
        self.assert_error(self.post("transactions", {"transactions": []}), 422)
        data = {"transactions": self.tx["transactions"][:1] * (LIMITS["transactions"] + 1)}
        self.assert_error(self.post("transactions", data), 413)

    def test_bad_numeric_and_identifier_types(self):
        for field, value in (("amount", True), ("amount", "100"), ("amount", float("nan")),
                             ("amount", float("inf")), ("amount", 10**500), ("step", -1),
                             ("step", 1.2), ("step", None), ("record_id", 7), ("type", "  ")):
            data = copy.deepcopy(self.tx)
            data["transactions"][0][field] = value
            with self.subTest(field=field, value_type=type(value).__name__):
                self.assert_error(self.post("transactions", data), 422)

    def test_duplicate_record_ids(self):
        data = copy.deepcopy(self.tx)
        data["transactions"][1]["record_id"] = data["transactions"][0]["record_id"]
        self.assert_error(self.post("transactions", data), 422)

    def test_nullable_numeric_and_negative_rules(self):
        data = copy.deepcopy(self.tx)
        data["transactions"][0]["amount"] = None
        data["transactions"][1]["amount"] = -100
        response = self.post("transactions", data)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()["predictions"]
        self.assertTrue(math.isfinite(result[0]["fraud_score"]))
        self.assertTrue(result[1]["rule_alert"])

    def test_ledger_each_sector_and_order(self):
        for sector in SECTORS:
            data = dict(self.ledger, sector=sector)
            response = self.post("ledger", data)
            with self.subTest(sector=sector):
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                result = response.get_json()
                self.assertEqual(result["count"], len(data["headers"]))
                self.assertEqual([p["document_id"] for p in result["predictions"]], [h["document_id"] for h in data["headers"]])
                self.assertEqual(sum(p["review_alert"] for p in result["predictions"]), 1)

    def test_ledger_equal_direct_library(self):
        data = self.ledger
        expected = self.bundle[1].predict(pd.DataFrame(data["headers"]), pd.DataFrame(data["lines"]),
            pd.DataFrame(data["chart_of_accounts"]), data["sector"], review_fraction=.01)
        self.assertEqual(self.post("ledger", data).get_json()["predictions"],
                         json.loads(expected.to_json(orient="records", double_precision=15)))

    def test_fixed_threshold_and_default_policy(self):
        fixed = self.post("ledger", dict(self.ledger, review_fraction=None)).get_json()
        self.assertEqual(fixed["review_policy"]["mode"], "fixed_validation_threshold")
        self.assertIsNone(fixed["review_policy"]["review_fraction"])
        data = copy.deepcopy(self.ledger)
        del data["review_fraction"]
        default = self.post("ledger", data).get_json()
        self.assertEqual(default["review_policy"]["review_fraction"], .01)
        self.assertEqual([r["anomaly_score"] for r in fixed["predictions"]],
                         [r["anomaly_score"] for r in default["predictions"]])

    def test_ledger_bad_sector_fraction_and_date(self):
        for patch in ({"sector": "unknown"}, {"sector": []}, {"review_fraction": 0},
                      {"review_fraction": True}, {"review_fraction": 1.1}):
            self.assert_error(self.post("ledger", dict(self.ledger, **patch)), 422)
        data = copy.deepcopy(self.ledger)
        data["headers"][0]["posting_date"] = "2025-02-30"
        self.assert_error(self.post("ledger", data), 422)

    def test_ledger_orphan_and_duplicate_header(self):
        data = copy.deepcopy(self.ledger)
        data["lines"][0]["document_id"] = "missing"
        self.assert_error(self.post("ledger", data), 422)
        data = copy.deepcopy(self.ledger)
        data["headers"].append(data["headers"][0])
        self.assert_error(self.post("ledger", data), 422)

    def test_duplicate_lines_preserved_and_flagged(self):
        data = copy.deepcopy(self.ledger)
        data["lines"].append(data["lines"][0])
        response = self.post("ledger", data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 3)
        self.assertIn("duplicate", response.get_json()["predictions"][0]["rule_reasons"])

    def test_missing_lines_and_empty_chart_retained(self):
        response = self.post("ledger", dict(self.ledger, lines=[], chart_of_accounts=[]))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        for row in response.get_json()["predictions"]:
            self.assertIn("no lines", row["rule_reasons"])
        response = self.post("ledger", dict(self.ledger, chart_of_accounts=[]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(row["rule_alert"] for row in response.get_json()["predictions"]))

    def test_invalid_document_date_and_null_amount_rule(self):
        data = copy.deepcopy(self.ledger)
        data["headers"][0]["document_date"] = None
        data["lines"][0]["debit_amount"] = None
        response = self.post("ledger", data)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        reasons = response.get_json()["predictions"][0]["rule_reasons"]
        self.assertIn("invalid document_date", reasons)
        self.assertIn("invalid line amount", reasons)

    def test_ledger_account_codes_require_strings(self):
        data = copy.deepcopy(self.ledger)
        data["lines"][0]["gl_account"] = 411000
        self.assert_error(self.post("ledger", data), 422)
        data = copy.deepcopy(self.ledger)
        data["chart_of_accounts"][0]["account_type"] = "unmapped"
        self.assert_error(self.post("ledger", data), 422)

    def test_openapi_fields_and_response_shapes(self):
        spec = json.loads((ROOT / "api/openapi.json").read_text(encoding="utf-8"))
        schemas = spec["components"]["schemas"]
        for name, fields in (("Transaction", TX), ("LedgerHeader", HEADERS), ("LedgerLine", LINES), ("Account", ACCOUNTS)):
            self.assertEqual(set(schemas[name]["required"]), set(fields))
        for kind, title, data in (("transactions", "Transactions", self.tx), ("ledger", "Ledger", self.ledger)):
            result = self.post(kind, data).get_json()
            self.assertEqual(set(result), set(schemas[title + "Response"]["required"]))
            item = "TransactionPrediction" if kind == "transactions" else "LedgerPrediction"
            self.assertEqual(set(result["predictions"][0]), set(schemas[item]["required"]))

    def test_busy_returns_429_without_second_model_call(self):
        entered, release = Event(), Event()
        original = self.bundle[0]
        class SlowModel:
            def predict(self, frame):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("Test release timed out")
                return original.predict(frame)
        app = create_app(api_token=TOKEN, bundle=(SlowModel(), *self.bundle[1:]))
        responses = []
        def first():
            with app.test_client() as client:
                responses.append(client.post("/v1/transactions/predict", json=self.tx, headers=AUTH))
        worker = Thread(target=first)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            with app.test_client() as client:
                response = client.post("/v1/transactions/predict", json=self.tx, headers=AUTH)
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.headers["Retry-After"], "1")
                self.assertEqual(client.get("/health").status_code, 200)
        finally:
            release.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(responses[0].status_code, 200)


if __name__ == "__main__":
    unittest.main()
