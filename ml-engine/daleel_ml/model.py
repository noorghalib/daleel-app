"""Train, persist, load, and call a standalone transaction-fraud classifier.

The model targets the dataset's ``isFraud`` transaction label. It does not
classify inventory errors, invoice mistakes, or accounting fraud in general.
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import confusion_matrix, precision_recall_fscore_support, precision_recall_curve
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, FunctionTransformer
from threadpoolctl import threadpool_limits

from .features import (
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    NUMERIC_FEATURES,
    REQUIRED_INPUT_COLUMNS,
    TARGET_COLUMN,
    build_features,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_PATH = Path(__file__).resolve().parent / "artifacts" / "fraud_detector.joblib"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "ml_data" / "reports"
RANDOM_STATE = 20261025
PREDICTION_COLUMNS = [
    "record_id",
    "prediction",
    "fraud_score",
    "rule_alert",
    "rule_reasons",
    "reason",
]


def _make_pipeline(model_name: str) -> Pipeline:
    """Create one candidate pipeline; identifiers and target never enter it."""
    numeric = Pipeline(
        steps=[("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
               ("float32", FunctionTransformer(np.asarray, kw_args={"dtype": np.float32}))]
    )
    categorical = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="most_frequent")),
            ("onehot", OneHotEncoder(handle_unknown="ignore", sparse_output=False, dtype=np.float32)),
        ]
    )
    preprocessing = ColumnTransformer(
        transformers=[
            ("numeric", numeric, NUMERIC_FEATURES),
            ("categorical", categorical, CATEGORICAL_FEATURES),
        ],
        remainder="drop",
    )

    if model_name == "random_forest":
        estimator = RandomForestClassifier(
            n_estimators=160,
            max_depth=16,
            min_samples_leaf=2,
            class_weight="balanced_subsample",
            n_jobs=8,
            random_state=RANDOM_STATE,
        )
    elif model_name == "hist_gradient_boosting":
        estimator = HistGradientBoostingClassifier(
            max_iter=140,
            learning_rate=0.08,
            max_leaf_nodes=23,
            l2_regularization=1.0,
            class_weight="balanced",
            early_stopping=True,
            random_state=RANDOM_STATE,
        )
    else:
        raise ValueError(f"Unsupported model: {model_name}")

    return Pipeline([("preprocess", preprocessing), ("classifier", estimator)])


def logical_rule_alerts(data: pd.DataFrame, tolerance: float = 0.02) -> pd.DataFrame:
    """Simple rule baseline for impossible values/source-balance arithmetic.

    These checks are intentionally limited and are not a general fraud detector.
    """
    missing = [column for column in REQUIRED_INPUT_COLUMNS if column not in data.columns]
    if missing:
        raise ValueError("Missing required transaction columns: " + ", ".join(missing))

    numeric = {}
    for column in [
        "amount",
        "oldbalanceOrg",
        "newbalanceOrig",
        "oldbalanceDest",
        "newbalanceDest",
    ]:
        numeric[column] = pd.to_numeric(data[column], errors="coerce").replace(
            [np.inf, -np.inf], np.nan
        )

    reasons = np.full(len(data), "", dtype=object)
    tx_type = data["type"].astype("string").str.upper().fillna("")
    def append_reason(mask, message):
        positions = np.flatnonzero(np.asarray(mask, dtype=bool))
        reasons[positions] = [f"{old}; {message}" if old else message for old in reasons[positions]]
    for column, values in numeric.items():
        append_reason((values < 0).fillna(False), f"{column} is negative")
    old_balance, amount, new_balance = (numeric[c] for c in ("oldbalanceOrg", "amount", "newbalanceOrig"))
    applies = tx_type.isin({"PAYMENT", "TRANSFER", "CASH_OUT", "DEBIT"}) & (old_balance >= amount)
    append_reason((applies & ((new_balance - (old_balance - amount)).abs() > tolerance)).fillna(False),
                  "origin balance does not match old balance minus amount")

    return pd.DataFrame(
        {"rule_alert": reasons != "", "rule_reasons": reasons}, index=data.index
    )


def calculate_metrics(y_true: pd.Series | np.ndarray, y_pred: pd.Series | np.ndarray) -> dict[str, float | int]:
    """Return binary fraud metrics with fraud (1) treated as the positive class."""
    truth = np.asarray(y_true, dtype=int)
    predicted = np.asarray(y_pred, dtype=int)
    if truth.shape != predicted.shape:
        raise ValueError("y_true and y_pred must have the same shape")
    if truth.size == 0:
        raise ValueError("Cannot calculate metrics for an empty test set")

    tn, fp, fn, tp = confusion_matrix(truth, predicted, labels=[0, 1]).ravel()
    precision, recall, f1, _ = precision_recall_fscore_support(
        truth,
        predicted,
        labels=[1],
        average=None,
        zero_division=0,
    )
    specificity = tn / (tn + fp) if (tn + fp) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    return {
        "TP": int(tp),
        "FP": int(fp),
        "TN": int(tn),
        "FN": int(fn),
        "precision": float(precision[0]),
        "recall": float(recall[0]),
        "f1": float(f1[0]),
        "specificity": float(specificity),
        "false_positive_rate": float(fpr),
        "alerts_total": int(predicted.sum()),
    }


def _choose_threshold(y_true: pd.Series, probabilities: np.ndarray) -> tuple[float, dict[str, Any]]:
    """Choose an F1 threshold using validation only; ties prefer higher precision."""
    probabilities = np.asarray(probabilities, dtype=float)
    if not len(probabilities) or not np.isfinite(probabilities).all():
        raise ValueError("Threshold selection requires finite, nonempty scores")
    precision, recall, thresholds = precision_recall_curve(y_true, probabilities)
    f1 = 2 * precision[:-1] * recall[:-1] / np.maximum(precision[:-1] + recall[:-1], 1e-15)
    best = np.lexsort((thresholds, precision[:-1], f1))[-1]
    threshold = float(thresholds[best])
    return threshold, calculate_metrics(y_true, probabilities >= threshold)


def temporal_split(data: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Split by ordered ``step`` values (70/15/15) without dividing one step."""
    if "step" not in data.columns:
        raise ValueError("A chronological split requires a 'step' column")
    steps = pd.to_numeric(data["step"], errors="coerce")
    if not np.isfinite(steps.to_numpy(dtype=float)).all():
        raise ValueError("Every row must have a finite step; rows are never silently dropped")
    counts = steps.value_counts().sort_index()
    unique_steps = counts.index.to_numpy()
    if len(unique_steps) < 3:
        raise ValueError("At least three distinct step values are needed for a temporal split")

    cumulative = counts.cumsum().to_numpy()
    train_end_index = int(np.argmin(np.abs(cumulative[:-2] - len(data) * .70)))
    permitted = np.arange(train_end_index + 1, len(unique_steps) - 1)
    validation_end_index = int(permitted[np.argmin(np.abs(cumulative[permitted] - len(data) * .85))])
    train_end = unique_steps[train_end_index]
    validation_end = unique_steps[validation_end_index]

    train_mask = steps <= train_end
    validation_mask = (steps > train_end) & (steps <= validation_end)
    test_mask = steps > validation_end
    return (
        np.flatnonzero(train_mask.to_numpy()),
        np.flatnonzero(validation_mask.to_numpy()),
        np.flatnonzero(test_mask.to_numpy()),
    )


def train_and_evaluate(
    data: pd.DataFrame,
    artifact_path: str | Path = DEFAULT_ARTIFACT_PATH,
    report_dir: str | Path = DEFAULT_REPORT_DIR,
) -> dict[str, Any]:
    """Compare two candidates on validation, test the winner once, then save it.

    The test partition is not used for model or threshold selection. The exact
    validation-calibrated pipeline is saved, without refitting it afterwards.
    """
    if TARGET_COLUMN not in data.columns:
        raise ValueError(f"Training data must include the '{TARGET_COLUMN}' target")
    if len(data) < 30:
        raise ValueError("At least 30 rows are required for this training experiment")

    frame = data.copy(deep=True)
    y = pd.to_numeric(frame[TARGET_COLUMN], errors="coerce")
    valid_target = y.isin([0, 1])
    if not valid_target.all():
        raise ValueError("Every training target must be 0 or 1; invalid rows are not dropped")
    y = y.astype(int)
    if y.nunique() < 2:
        raise ValueError("Training data must contain both normal and fraud labels")

    train_index, validation_index, test_index = temporal_split(frame)
    if min(len(train_index), len(validation_index), len(test_index)) == 0:
        raise ValueError("The temporal split produced an empty partition")
    y_train = y.iloc[train_index]
    y_validation = y.iloc[validation_index]
    y_test = y.iloc[test_index]
    for name, part in [("training", y_train), ("validation", y_validation), ("test", y_test)]:
        if part.nunique() < 2:
            raise ValueError(
                f"The {name} split has only one target class; obtain a broader temporal sample."
            )

    X = build_features(frame)
    # Explicit assertion so future feature edits cannot silently use a label or ID.
    forbidden = {TARGET_COLUMN, "isFlaggedFraud", "nameOrig", "nameDest"}
    if forbidden.intersection(X.columns):
        raise AssertionError("A target, fraud flag, or account identifier entered the features")

    candidate_results: dict[str, dict[str, Any]] = {}
    candidates = ["random_forest", "hist_gradient_boosting"]
    for model_name in candidates:
        candidate = _make_pipeline(model_name)
        with threadpool_limits(limits=8):
            candidate.fit(X.iloc[train_index], y_train)
            probabilities = candidate.predict_proba(X.iloc[validation_index])[:, 1]
        threshold, validation_metrics = _choose_threshold(y_validation, probabilities)
        candidate_results[model_name] = {
            "pipeline": candidate,
            "threshold": threshold,
            "validation_metrics": validation_metrics,
        }

    winner_name = max(
        candidates,
        key=lambda name: (
            candidate_results[name]["validation_metrics"]["f1"],
            candidate_results[name]["validation_metrics"]["precision"],
        ),
    )
    chosen = candidate_results[winner_name]
    chosen_threshold = chosen["threshold"]

    final_pipeline = chosen["pipeline"]

    test_probabilities = final_pipeline.predict_proba(X.iloc[test_index])[:, 1]
    test_predictions = (test_probabilities >= chosen_threshold).astype(int)
    model_metrics = calculate_metrics(y_test, test_predictions)

    test_data = frame.iloc[test_index]
    rules = logical_rule_alerts(test_data)
    rule_metrics = calculate_metrics(y_test, rules["rule_alert"].astype(int))

    artifact = {
        "pipeline": final_pipeline,
        "metadata": {
            "task": "binary financial transaction fraud classification",
            "target": TARGET_COLUMN,
            "selected_model": winner_name,
            "threshold": chosen_threshold,
            "random_state": RANDOM_STATE,
            "feature_columns": FEATURE_COLUMNS,
            "excluded_columns": sorted(
                ["nameOrig", "nameDest", "isFlaggedFraud", "isFraud", "record_id"]
            ),
            "score_semantics": "Raw predict_proba score for isFraud=1; not calibrated confidence.",
            "source_dataset": "RegularGuyOIO/financial-transaction-fraud-dataset",
        },
    }
    artifact_file = Path(artifact_path)
    artifact_file.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, artifact_file, compress=3)
    artifact["metadata"]["artifact_bytes"] = artifact_file.stat().st_size

    output_dir = Path(report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = artifact_file.with_suffix(".metadata.json")
    metadata_path.write_text(
        json.dumps(artifact["metadata"], ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Save only concise false-positive / false-negative audit examples.
    audit = test_data.copy()
    if "sample_row_id" not in audit.columns:
        audit.insert(0, "sample_row_id", pd.Series(test_index, index=test_data.index))
    audit["truth"] = y_test.to_numpy()
    audit["predicted"] = test_predictions
    audit["fraud_score"] = test_probabilities
    audit["rule_alert"] = rules["rule_alert"].to_numpy()
    audit["rule_reasons"] = rules["rule_reasons"].to_numpy()
    safe_audit_columns = [
        "sample_row_id",
        "step",
        "type",
        "amount",
        "truth",
        "predicted",
        "fraud_score",
        "rule_alert",
        "rule_reasons",
    ]
    audit.loc[(audit["truth"] == 0) & (audit["predicted"] == 1), safe_audit_columns].head(100).to_csv(
        output_dir / "false_positives.csv", index=False
    )
    audit.loc[(audit["truth"] == 1) & (audit["predicted"] == 0), safe_audit_columns].head(100).to_csv(
        output_dir / "false_negatives.csv", index=False
    )

    result = {
        "source_rows": int(len(frame)),
        "train_rows": int(len(train_index)),
        "validation_rows": int(len(validation_index)),
        "test_rows": int(len(test_index)),
        "train_positive": int(y_train.sum()),
        "validation_positive": int(y_validation.sum()),
        "test_positive": int(y_test.sum()),
        "random_state": RANDOM_STATE,
        "candidate_validation": {
            name: {
                "threshold": candidate_results[name]["threshold"],
                "metrics": candidate_results[name]["validation_metrics"],
            }
            for name in candidates
        },
        "selected_model": winner_name,
        "selected_threshold": chosen_threshold,
        "test_metrics": {"logical_rules": rule_metrics, winner_name: model_metrics},
        "artifact_path": str(artifact_file),
        "artifact_bytes": int(artifact_file.stat().st_size),
        "metadata_path": str(metadata_path),
        "features": FEATURE_COLUMNS,
    }
    (output_dir / "evaluation.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


class DaleelFraudModel:
    """Loaded, reusable prediction wrapper suitable for application startup."""

    def __init__(self, pipeline: Pipeline, metadata: dict[str, Any]):
        self.pipeline = pipeline
        self.metadata = metadata

    def predict(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return stable row IDs, predictions, scores, and actual rule reasons."""
        features = build_features(data)
        if data.empty:
            return pd.DataFrame(columns=PREDICTION_COLUMNS)
        with threadpool_limits(limits=8):
            scores = self.pipeline.predict_proba(features)[:, 1]
        threshold = float(self.metadata["threshold"])
        predictions = scores >= threshold
        rules = logical_rule_alerts(data)

        if "record_id" in data.columns:
            record_ids = data["record_id"].reset_index(drop=True)
        elif "sample_row_id" in data.columns:
            record_ids = data["sample_row_id"].reset_index(drop=True)
        else:
            record_ids = pd.Series(data.index.astype(str), dtype="string")

        reasons = []
        for flagged, rule_reason in zip(predictions, rules["rule_reasons"].tolist()):
            if rule_reason:
                reasons.append("قاعدة تحقق: " + rule_reason)
            elif flagged:
                reasons.append("لا يتوفر تفسير سببي للنموذج؛ يلزم فحص المعاملة يدوياً.")
            else:
                reasons.append("")

        return pd.DataFrame(
            {
                "record_id": record_ids.to_numpy(),
                "prediction": np.where(predictions, "Anomaly", "Normal"),
                "fraud_score": scores.astype(float),
                "rule_alert": rules["rule_alert"].to_numpy(dtype=bool),
                "rule_reasons": rules["rule_reasons"].to_numpy(),
                "reason": reasons,
            },
            columns=PREDICTION_COLUMNS,
        )


def load_model(path: str | Path = DEFAULT_ARTIFACT_PATH) -> DaleelFraudModel:
    """Load the saved model once at application startup; does not retrain."""
    artifact_path = Path(path)
    if not artifact_path.exists():
        raise FileNotFoundError(
            f"Saved model not found at {artifact_path}. Run `python -m daleel_ml.train` first."
        )
    artifact = joblib.load(artifact_path)
    if not isinstance(artifact, dict) or "pipeline" not in artifact or "metadata" not in artifact:
        raise ValueError("The artifact does not contain the expected Daleel model payload")
    return DaleelFraudModel(artifact["pipeline"], artifact["metadata"])


@lru_cache(maxsize=4)
def _cached_model(path: str) -> DaleelFraudModel:
    return load_model(path)


def predict(data: pd.DataFrame, model_path: str | Path = DEFAULT_ARTIFACT_PATH) -> pd.DataFrame:
    """Convenience entry point; the persisted artifact is cached between calls."""
    return _cached_model(str(Path(model_path).resolve())).predict(data)
