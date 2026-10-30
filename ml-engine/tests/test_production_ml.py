import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import joblib
import numpy as np
import pandas as pd
import requests
from sklearn.ensemble import IsolationForest

from daleel_ml.download import download_file
from daleel_ml.experiment import run_child
from daleel_ml.features import build_features
from daleel_ml.ledger import (aggregate_ledger, LedgerEncoder, DaleelLedgerModel,
                              load_ledger_model, review_mask, NUMERIC, CATEGORICAL)
from daleel_ml.model import (_choose_threshold, calculate_metrics, temporal_split,
                             logical_rule_alerts, train_and_evaluate, load_model)
from test_ml_model import make_labeled_transactions


def ledger_fixture(rows=120):
    headers = pd.DataFrame({"document_id": [f"doc-{i:04}" for i in range(rows)],
        "posting_date": pd.date_range("2024-01-01", periods=rows),
        "document_date": pd.date_range("2024-01-01", periods=rows),
        "document_type": "SA", "source": "manual", "business_process": "O2C"})
    lines = pd.DataFrame([{"document_id": f"doc-{i:04}", "line_number": j,
        "gl_account": "411000" if j == 1 else "701000", "debit_amount": float(100+i) if j == 1 else 0.,
        "credit_amount": float(100+i) if j == 2 else 0., "local_amount": float(100+i) * (1 if j == 1 else -1),
        "auxiliary_account_number": "customer1"} for i in range(rows) for j in (1, 2)])
    accounts = pd.DataFrame({"account_number": ["411000", "701000"], "account_type": ["Tiers", "Revenue"]})
    return headers, lines, accounts


class TimeAndSelectionTests(unittest.TestCase):
    def test_threshold_matches_exhaustive_search_with_ties(self):
        rng = np.random.default_rng(42)
        y = rng.integers(0, 2, 500)
        scores = rng.integers(0, 50, 500) / 49
        threshold, metrics = _choose_threshold(y, scores)
        candidates = []
        for value in np.unique(scores):
            m = calculate_metrics(y, scores >= value)
            candidates.append((m["f1"], m["precision"], value))
        expected = max(candidates)
        self.assertEqual(threshold, expected[2])
        self.assertAlmostEqual(metrics["f1"], expected[0])

    def test_split_preserves_all_rows_and_complete_time_groups(self):
        frame = pd.DataFrame({"step": np.repeat(np.arange(12), [100, 2, 7, 90, 25, 60, 5, 13, 70, 14, 8, 40])})
        parts = temporal_split(frame)
        self.assertEqual(sorted(np.concatenate(parts).tolist()), list(range(len(frame))))
        times = [set(frame.iloc[p].step) for p in parts]
        self.assertTrue(max(times[0]) < min(times[1]) < min(times[2]))
        self.assertFalse(times[0] & times[1] or times[1] & times[2])

    def test_invalid_time_is_rejected_instead_of_dropped(self):
        with self.assertRaises(ValueError):
            temporal_split(pd.DataFrame({"step": [1, 2, 3, np.nan]}))

    def test_rules_support_duplicate_dataframe_indices(self):
        data = make_labeled_transactions(30)
        data.index = [0] * len(data)
        result = logical_rule_alerts(data)
        self.assertEqual(len(result), len(data))

    def test_saved_model_is_exact_validation_calibrated_model(self):
        data = make_labeled_transactions()
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "model.joblib"
            result = train_and_evaluate(data, artifact, Path(temp) / "reports")
            model = load_model(artifact)
            _, validation, _ = temporal_split(data)
            scores = model.pipeline.predict_proba(build_features(data.iloc[validation]))[:, 1]
            measured = calculate_metrics(data.iloc[validation].isFraud, scores >= model.metadata["threshold"])
            self.assertEqual(measured, result["candidate_validation"][result["selected_model"]]["metrics"])
            self.assertEqual(len(model.predict(data.head(0))), 0)

    def test_invalid_targets_are_rejected(self):
        data = make_labeled_transactions()
        data.loc[0, "isFraud"] = 2
        with self.assertRaises(ValueError):
            train_and_evaluate(data)

    def test_worker_timeout_and_failure_are_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run_child([sys.executable, "-c", "import time; time.sleep(10)"], Path(temp) / "timeout.log", .1)
            self.assertEqual(result["status"], "timed_out")
            result = run_child([sys.executable, "-c", "raise RuntimeError('expected')"], Path(temp) / "failed.log", 10)
            self.assertEqual(result["status"], "failed")


class LedgerSafetyTests(unittest.TestCase):
    def test_join_retains_headers_without_lines_and_reports_them(self):
        h, l, a = ledger_fixture()
        result = aggregate_ledger(h, l[l.document_id != h.document_id.iloc[0]], a)
        self.assertEqual(len(result), len(h))
        self.assertEqual(result.document_id.tolist(), h.document_id.tolist())
        self.assertIn("no lines", result.rule_reasons.iloc[0])

    def test_orphan_lines_fail(self):
        h, l, a = ledger_fixture()
        with self.assertRaises(ValueError):
            aggregate_ledger(h, l.assign(document_id="unknown"), a)

    def test_duplicate_line_keys_are_retained_and_reported(self):
        h, l, a = ledger_fixture()
        repeated = pd.concat([l, l.head(1)], ignore_index=True)
        result = aggregate_ledger(h, repeated, a)
        self.assertEqual(int(result.line_count.sum()), len(repeated))
        self.assertIn("duplicate document/line key", result.rule_reasons.iloc[0])
        self.assertEqual(result.duplicate_line_key_count.iloc[0], 2)

    def test_encoding_uses_training_only_and_ignores_labels_and_ids(self):
        frame = aggregate_ledger(*ledger_fixture())
        encoder = LedgerEncoder().fit(frame.head(70))
        changed = frame.tail(20).copy()
        changed["document_type"] = "UNSEEN"
        changed["isFraud"] = 1
        changed["document_id"] = "different"
        features = encoder._raw(changed)
        self.assertTrue(features.document_type_frequency.eq(0).all())
        self.assertEqual(list(features), NUMERIC + [c + "_frequency" for c in CATEGORICAL])
        before = encoder.transform(changed)
        changed.isFraud = 0
        np.testing.assert_array_equal(before, encoder.transform(changed))

    def test_top_fraction_is_deterministic_and_validated(self):
        scores = np.array([.8, .8, .1, .9])
        ids = np.array(["b", "a", "c", "d"])
        self.assertEqual(set(ids[review_mask(scores, ids, .5)]), {"a", "d"})
        for fraction in (0, -1, 1.1):
            with self.assertRaises(ValueError):
                review_mask(scores, ids, fraction)

    def test_saved_ledger_predictions_match_and_rules_are_separate(self):
        h, l, a = ledger_fixture()
        frame = aggregate_ledger(h, l, a)
        encoder = LedgerEncoder().fit(frame.head(70))
        model = IsolationForest(n_estimators=10, max_samples=64, n_jobs=1, random_state=3).fit(encoder.transform(frame.head(70)))
        payload = {"models": {"energy": {"encoder": encoder, "model": model, "threshold": .6}}, "metadata": {"labels_available": False}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ledger.joblib"
            joblib.dump(payload, path)
            first = DaleelLedgerModel(payload).predict(h, l, a, "energy")
            second = load_ledger_model(path).predict(h, l, a, "energy")
            pd.testing.assert_frame_equal(first, second)
            self.assertEqual(int(first.review_alert.sum()), 2)
            self.assertFalse(first.rule_alert.any())
            self.assertNotIn("fraud_score", first)
            with self.assertRaises(ValueError):
                load_ledger_model(path).predict(h, l, a, "invalid")


class Response:
    def __init__(self, payload, status=200, headers=None, fail=False):
        self.payload, self.status_code, self.headers, self.fail = payload, status, headers or {}, fail
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))
    def iter_content(self, block):
        yield self.payload
        if self.fail:
            raise requests.ConnectionError("interrupted")


class DownloadRecoveryTests(unittest.TestCase):
    def test_interruption_resumes_verified_range(self):
        payload = b"abcdef"
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "data.csv"
            with patch("daleel_ml.download.requests.get", side_effect=[Response(b"abc", fail=True),
                Response(b"def", 206, {"Content-Range": "bytes 3-5/6"})]) as get, patch("daleel_ml.download.time.sleep"):
                digest = download_file("https://example.test/file", destination, 6, hashlib.sha256(payload).hexdigest())
                self.assertEqual(get.call_args_list[1].kwargs["headers"]["Range"], "bytes=3-")
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())

    def test_server_ignoring_range_restarts_cleanly(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data.csv"
            path.with_suffix(".csv.part").write_bytes(b"bad")
            with patch("daleel_ml.download.requests.get", return_value=Response(b"abcdef")):
                download_file("https://example.test/file", path, 6)
            self.assertEqual(path.read_bytes(), b"abcdef")

    def test_corrupt_file_and_invalid_range_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data.csv"
            with patch("daleel_ml.download.requests.get", return_value=Response(b"wrong!")):
                with self.assertRaises(RuntimeError):
                    download_file("https://example.test/file", path, 6, "0" * 64, retries=1)
            self.assertFalse(path.exists())
            path.with_suffix(".csv.part").write_bytes(b"abc")
            with patch("daleel_ml.download.requests.get", return_value=Response(b"def", 206, {"Content-Range": "bytes 0-2/6"})):
                with self.assertRaises(RuntimeError):
                    download_file("https://example.test/file", path, 6, retries=1)


if __name__ == "__main__":
    unittest.main()
