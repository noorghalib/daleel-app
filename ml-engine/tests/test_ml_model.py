import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from daleel_ml.features import FEATURE_COLUMNS, build_features
from daleel_ml.model import (
    DaleelFraudModel,
    calculate_metrics,
    load_model,
    logical_rule_alerts,
    train_and_evaluate,
)


def make_labeled_transactions(rows=600):
    """Create deterministic, time-ordered data for offline model tests."""
    output = []
    for i in range(rows):
        step = (i // 10) + 1
        fraud = int(i % 10 == 0 and step % 2 == 0)
        amount = float(180 + (i % 31) * 9)
        old_balance = amount + 400 + (i % 101)
        new_balance = old_balance - amount
        if fraud:
            # Deliberate source-balance anomaly, separate from the label column.
            new_balance = 0.0
        output.append(
            {
                "record_id": f"row-{i}",
                "step": step,
                "type": "TRANSFER" if i % 2 else "PAYMENT",
                "amount": amount,
                "nameOrig": f"C{i % 17}",
                "oldbalanceOrg": old_balance,
                "newbalanceOrig": new_balance,
                "nameDest": f"C{i % 23}",
                "oldbalanceDest": 2000.0 + i,
                "newbalanceDest": 2000.0 + i + amount,
                "isFraud": fraud,
                "isFlaggedFraud": fraud,
            }
        )
    return pd.DataFrame(output)


class FeatureSafetyTests(unittest.TestCase):
    def test_target_flags_and_identifiers_are_not_features(self):
        features = build_features(make_labeled_transactions(20))
        self.assertEqual(list(features.columns), FEATURE_COLUMNS)
        self.assertNotIn("isFraud", features.columns)
        self.assertNotIn("isFlaggedFraud", features.columns)
        self.assertNotIn("nameOrig", features.columns)
        self.assertNotIn("nameDest", features.columns)
        self.assertNotIn("record_id", features.columns)

    def test_missing_and_invalid_numeric_values_are_preserved_for_pipeline_imputation(self):
        data = make_labeled_transactions(20)
        data.loc[0, "amount"] = "not-a-number"
        data.loc[1, "oldbalanceOrg"] = np.inf
        data.loc[2, "type"] = None
        features = build_features(data)
        self.assertTrue(pd.isna(features.loc[0, "amount"]))
        self.assertTrue(pd.isna(features.loc[1, "oldbalanceOrg"]))
        self.assertEqual(features.loc[2, "type"], "__MISSING__")
        self.assertEqual(len(features), len(data))

    def test_metric_calculation(self):
        metrics = calculate_metrics([0, 0, 1, 1], [0, 1, 1, 0])
        self.assertEqual((metrics["TP"], metrics["FP"], metrics["TN"], metrics["FN"]), (1, 1, 1, 1))
        self.assertAlmostEqual(metrics["precision"], 0.5)
        self.assertAlmostEqual(metrics["recall"], 0.5)
        self.assertAlmostEqual(metrics["f1"], 0.5)
        self.assertAlmostEqual(metrics["false_positive_rate"], 0.5)


class TrainingAndPredictionTests(unittest.TestCase):
    def test_logical_baseline_finds_balance_arithmetic_issue_without_using_label(self):
        data = make_labeled_transactions(50)
        data.loc[1, "newbalanceOrig"] = 0
        result = logical_rule_alerts(data)
        self.assertTrue(result.loc[1, "rule_alert"])
        self.assertIn("origin balance", result.loc[1, "rule_reasons"])

    def test_train_save_load_reuse_and_stable_output_shape(self):
        data = make_labeled_transactions(600)
        original = data.copy(deep=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "fraud_detector.joblib"
            report_dir = root / "reports"
            result = train_and_evaluate(data, artifact_path=artifact, report_dir=report_dir)
            self.assertTrue(artifact.exists())
            self.assertIn(result["selected_model"], {"random_forest", "hist_gradient_boosting"})
            self.assertEqual(result["test_rows"] + result["train_rows"] + result["validation_rows"], len(data))
            self.assertEqual(set(result["test_metrics"]), {"logical_rules", result["selected_model"]})
            self.assertEqual(artifact.stat().st_size, result["artifact_bytes"])

            model = load_model(artifact)
            batch = data.head(8).copy()
            first = model.predict(batch)
            second = model.predict(batch)
            self.assertIsInstance(model, DaleelFraudModel)
            self.assertEqual(list(first.columns), [
                "record_id", "prediction", "fraud_score", "rule_alert", "rule_reasons", "reason"
            ])
            self.assertEqual(len(first), len(batch))
            pd.testing.assert_frame_equal(first, second)
            self.assertTrue(first["prediction"].isin(["Normal", "Anomaly"]).all())
            self.assertTrue(first["fraud_score"].between(0, 1).all())

            changed_ids = batch.copy()
            changed_ids["nameOrig"] = "different-account"
            changed_ids["nameDest"] = "different-destination"
            changed_ids["record_id"] = "new-id"
            changed_result = model.predict(changed_ids)
            np.testing.assert_allclose(first["fraud_score"], changed_result["fraud_score"])
            self.assertEqual(first["prediction"].tolist(), changed_result["prediction"].tolist())
            pd.testing.assert_frame_equal(data, original)


if __name__ == "__main__":
    unittest.main()
