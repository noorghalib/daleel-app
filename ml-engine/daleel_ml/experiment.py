"""Full-data CPU experiment with isolated sequential trials and hard time budgets."""
from __future__ import annotations

import argparse
import gc
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import joblib
import numpy as np
import pandas as pd
import psutil
import pyarrow as pa
import pyarrow.parquet as pq
from sklearn.metrics import average_precision_score, auc, precision_recall_curve
from threadpoolctl import threadpool_limits

from .download import sha256, SOURCES
from .features import build_features, REQUIRED_INPUT_COLUMNS, FEATURE_COLUMNS
from .model import (_make_pipeline, _choose_threshold, calculate_metrics, temporal_split,
                    logical_rule_alerts, DEFAULT_ARTIFACT_PATH, RANDOM_STATE)

pa.set_cpu_count(8)
pa.set_io_thread_count(2)

PACKAGES = ["numpy", "pandas", "scikit-learn", "joblib", "pyarrow", "xgboost", "threadpoolctl"]
TRIALS = [
    {"name": "random_forest", "family": "random_forest", "params": {}},
    {"name": "hist_gradient_boosting", "family": "hist_gradient_boosting", "params": {}},
    {"name": "xgboost_economic", "family": "xgboost", "params": {"n_estimators": 400, "max_depth": 4, "learning_rate": .08}},
    {"name": "xgboost_deep", "family": "xgboost", "params": {"n_estimators": 800, "max_depth": 6, "learning_rate": .05}},
    {"name": "xgboost_regularized", "family": "xgboost", "params": {"n_estimators": 1000, "max_depth": 5, "learning_rate": .04, "reg_lambda": 10., "min_child_weight": 5}},
    {"name": "hist_gradient_boosting_extended", "family": "hist_gradient_boosting", "params": {"max_iter": 400, "max_leaf_nodes": 31, "learning_rate": .05}},
]


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def versions():
    return {name: importlib.metadata.version(name) for name in PACKAGES}


def score_metrics(y, scores, threshold):
    metrics = calculate_metrics(y, scores >= threshold)
    precision, recall, _ = precision_recall_curve(y, scores)
    metrics.update(average_precision=float(average_precision_score(y, scores)),
                   pr_auc=float(auc(recall, precision)))
    return metrics


def prepare(source, work, reports):
    """Two chunked passes; group steps at row-quantiles, keeping prevalence intact."""
    source, work, reports = Path(source), Path(work), Path(reports)
    work.mkdir(parents=True, exist_ok=True)
    counts, positives = {}, {}
    for chunk in pd.read_csv(source, usecols=["step", "isFraud"], chunksize=250_000):
        if not chunk["isFraud"].isin([0, 1]).all() or not np.isfinite(chunk["step"]).all():
            raise ValueError("Invalid target or step in full dataset")
        for step, group in chunk.groupby("step"):
            counts[int(step)] = counts.get(int(step), 0) + len(group)
            positives[int(step)] = positives.get(int(step), 0) + int(group.isFraud.sum())
    groups = pd.Series(counts).sort_index()
    # Expand just 743 step groups logically, avoiding a large split-only DataFrame.
    cumulative = groups.cumsum().to_numpy()
    n = int(groups.sum())
    first = int(np.argmin(np.abs(cumulative[:-2] - .70 * n)))
    allowed = np.arange(first + 1, len(groups) - 1)
    second = int(allowed[np.argmin(np.abs(cumulative[allowed] - .85 * n))])
    train_end, validation_end = int(groups.index[first]), int(groups.index[second])
    writers, statistics = {}, {p: {"rows": 0, "positive": 0} for p in ("train", "validation", "test")}
    offset = 0
    try:
        for chunk in pd.read_csv(source, usecols=REQUIRED_INPUT_COLUMNS + ["isFraud"], chunksize=200_000):
            features = build_features(chunk)
            for col in FEATURE_COLUMNS:
                features[col] = features[col].astype("string" if col == "type" else "float32")
            features["isFraud"] = chunk.isFraud.astype("int8")
            features["_row_id"] = np.arange(offset, offset + len(chunk), dtype=np.int64)
            masks = {"train": chunk.step <= train_end,
                     "validation": (chunk.step > train_end) & (chunk.step <= validation_end),
                     "test": chunk.step > validation_end}
            for part, mask in masks.items():
                selected = features.loc[mask]
                if selected.empty:
                    continue
                table = pa.Table.from_pandas(selected, preserve_index=False)
                if part not in writers:
                    writers[part] = pq.ParquetWriter(work / f"{part}.parquet", table.schema, compression="zstd")
                writers[part].write_table(table)
                statistics[part]["rows"] += len(selected)
                statistics[part]["positive"] += int(selected.isFraud.sum())
            offset += len(chunk)
            print(f"Prepared {offset:,}/{n:,} transactions", flush=True)
    finally:
        for writer in writers.values():
            writer.close()
    for part, stats in statistics.items():
        if not 0 < stats["positive"] < stats["rows"]:
            raise ValueError(f"Both classes are required in {part}: {stats}")
        stats["prevalence"] = stats["positive"] / stats["rows"]
    report = {"source_rows": n, "source_path": str(source), "source_sha256": sha256(source), "source_bytes": source.stat().st_size,
              "dataset": SOURCES["transactions"][0], "revision": SOURCES["transactions"][1],
              "train_last_step": train_end, "validation_last_step": validation_end,
              "partitions": statistics, "features": FEATURE_COLUMNS,
              "split_policy": "Row quantiles at complete chronological step boundaries; no resampling"}
    write_json(reports / "data_validation.json", report)
    return report


def build_candidate(config, y):
    family = config["family"]
    pipeline = _make_pipeline(family if family != "xgboost" else "hist_gradient_boosting")
    if family == "xgboost":
        from xgboost import XGBClassifier
        ratio = float((y == 0).sum() / (y == 1).sum())
        pipeline.set_params(classifier=XGBClassifier(tree_method="hist", device="cpu", n_jobs=8,
            objective="binary:logistic", eval_metric="aucpr", random_state=RANDOM_STATE,
            scale_pos_weight=ratio, subsample=.9, colsample_bytree=.9, **config["params"]))
    else:
        pipeline.set_params(**{f"classifier__{key}": val for key, val in config["params"].items()})
    return pipeline


def trial_worker(work, trial_index):
    config = TRIALS[trial_index]
    destination = work / "trials" / config["name"]
    destination.mkdir(parents=True, exist_ok=True)
    train = pd.read_parquet(work / "train.parquet")
    y = train.pop("isFraud").to_numpy()
    train.drop(columns="_row_id", inplace=True)
    pipeline = build_candidate(config, y)
    begin = time.monotonic()
    with threadpool_limits(limits=8):
        pipeline.fit(train, y)
    fit_seconds = time.monotonic() - begin
    del train, y
    gc.collect()
    validation = pd.read_parquet(work / "validation.parquet")
    y = validation.pop("isFraud").to_numpy()
    validation.drop(columns="_row_id", inplace=True)
    begin = time.monotonic()
    with threadpool_limits(limits=8):
        scores = pipeline.predict_proba(validation)[:, 1]
    predict_seconds = time.monotonic() - begin
    threshold, _ = _choose_threshold(y, scores)
    metrics = score_metrics(y, scores, threshold)
    metadata = {"task": "binary financial transaction fraud classification", "target": "isFraud",
                "selected_model": config["name"], "family": config["family"], "threshold": threshold,
                "random_state": RANDOM_STATE, "feature_columns": FEATURE_COLUMNS,
                "excluded_columns": ["isFraud", "isFlaggedFraud", "nameOrig", "nameDest", "record_id"],
                "source_dataset": SOURCES["transactions"][0], "revision": SOURCES["transactions"][1],
                "versions": versions(), "parameters": config["params"],
                "score_semantics": "Raw predict_proba score; not calibrated confidence",
                "selection": "Validation F1, precision, then shorter validation prediction time; no post-selection refit",
                "numeric_storage": "float32 after original-precision feature derivation"}
    joblib.dump({"pipeline": pipeline, "metadata": metadata}, destination / "model.joblib", compress=3)
    write_json(destination / "result.json", {"name": config["name"], "status": "completed",
        "threshold": threshold, "validation_metrics": metrics, "fit_seconds": fit_seconds,
        "validation_predict_seconds": predict_seconds, "parameters": config["params"]})


def run_child(command, log_path, timeout):
    env = os.environ.copy()
    env.update({key: "8" for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")})
    env["PYTHONUNBUFFERED"] = "1"
    begin, peak = time.monotonic(), 0
    with Path(log_path).open("w", encoding="utf-8") as log:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
        process = psutil.Process(child.pid)
        timed_out = False
        while child.poll() is None:
            try:
                # Windows venv launchers spawn the real interpreter as a child.
                members = [process, *process.children(recursive=True)]
                rss = 0
                for member in members:
                    try:
                        rss += member.memory_info().rss
                    except psutil.Error:
                        pass
                peak = max(peak, rss)
            except psutil.Error:
                pass
            if time.monotonic() - begin > timeout:
                try:
                    descendants = process.children(recursive=True)
                except psutil.Error:
                    descendants = []
                for member in reversed(descendants):
                    try:
                        member.kill()
                    except psutil.Error:
                        pass
                child.kill()
                timed_out = True
                break
            time.sleep(.25)
        child.wait()
    return {"status": "timed_out" if timed_out else ("completed" if child.returncode == 0 else "failed"),
            "returncode": child.returncode, "wall_seconds": time.monotonic() - begin, "peak_rss_bytes": peak}


def evaluate_locked(work, reports, selected, completed, artifact):
    """Selection is already persisted. Test metrics cannot influence promotion."""
    chosen_path = work / "trials" / selected["name"] / "model.joblib"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(chosen_path, artifact)
    model = joblib.load(artifact)
    write_json(artifact.with_suffix(".metadata.json"), model["metadata"])
    split = json.loads((reports / "data_validation.json").read_text(encoding="utf-8"))
    test = pd.read_parquet(work / "test.parquet")
    y = test.pop("isFraud").to_numpy()
    ids = test.pop("_row_id").to_numpy()
    comparisons = {}
    test_names = list(dict.fromkeys([name for name in ("random_forest", "hist_gradient_boosting", selected["name"])
                                    if name in {c["name"] for c in completed}]))
    for name in test_names:
        payload = model if name == selected["name"] else joblib.load(work / "trials" / name / "model.joblib")
        begin = time.monotonic()
        with threadpool_limits(limits=8):
            scores = payload["pipeline"].predict_proba(test)[:, 1]
        threshold = payload["metadata"]["threshold"]
        comparisons[name] = score_metrics(y, scores, threshold)
        comparisons[name]["predict_seconds"] = time.monotonic() - begin
        if name == selected["name"]:
            audit = test[["step", "type", "amount"]].copy()
            audit.insert(0, "sample_row_id", ids)
            audit["truth"], audit["predicted"], audit["fraud_score"] = y, (scores >= threshold).astype(int), scores
            for filename, mask in (("false_positives", (y == 0) & (scores >= threshold)),
                                   ("false_negatives", (y == 1) & (scores < threshold))):
                audit.loc[mask].head(100).to_csv(reports / f"{filename}.csv", index=False)
            precision, recall, _ = precision_recall_curve(y, scores)
            take = np.linspace(0, len(precision)-1, min(1000, len(precision)), dtype=int)
            pd.DataFrame({"precision": precision[take], "recall": recall[take]}).to_csv(reports / "pr_curve.csv", index=False)
        del payload
        gc.collect()
    # Rule arithmetic must retain original cents rather than float32 model storage.
    source = Path(split.get("source_path", reports.parent.parent / "raw" / "transactions" / "Fraud.csv"))
    raw_test = pd.concat([chunk.loc[chunk.step > split["validation_last_step"]]
        for chunk in pd.read_csv(source, usecols=REQUIRED_INPUT_COLUMNS, chunksize=250_000)], ignore_index=True)
    if len(raw_test) != len(test):
        raise ValueError("Raw and processed test rows do not match")
    rules = logical_rule_alerts(raw_test)
    comparisons["logical_rules"] = calculate_metrics(y, rules.rule_alert.to_numpy(dtype=int))
    result = {"selected_model": selected["name"], "selected_threshold": selected["threshold"],
              "selected_on": "validation_only", "refit_after_threshold_selection": False,
              "source_rows": split["source_rows"], "partitions": split["partitions"],
              "candidate_validation": completed, "test_metrics": comparisons,
              "artifact_bytes": artifact.stat().st_size, "artifact_sha256": sha256(artifact),
              "versions": versions(), "split": split}
    write_json(reports / "evaluation.json", result)
    pd.DataFrame(comparisons).T.to_csv(reports / "test_comparison.csv", index_label="model")
    print(json.dumps({"winner": selected["name"], "test_metrics": comparisons[selected["name"]]}, indent=2), flush=True)
    return result


def run_experiment(source, work, reports, artifact, hours=6.):
    work, reports, artifact = Path(work), Path(reports), Path(artifact)
    reports.mkdir(parents=True, exist_ok=True)
    prepare(source, work, reports)
    start = time.monotonic()
    deadline = start + hours * 3600
    events, completed = [], []
    for index, config in enumerate(TRIALS):
        remaining = deadline - time.monotonic()
        if remaining < 900:
            events.append({"name": config["name"], "status": "skipped_budget"})
            continue
        print(f"Training {config['name']} on full training partition; {remaining / 60:.1f} min left", flush=True)
        status = run_child([sys.executable, "-m", "daleel_ml.experiment", "--worker", str(index), "--work", str(work)],
                           reports / f"{config['name']}.log", min(3600, remaining - 600))
        event = {"name": config["name"], **status}
        if status["status"] == "completed":
            path = work / "trials" / config["name"] / "result.json"
            event.update(json.loads(path.read_text(encoding="utf-8")))
            completed.append(event.copy())
            print(f"{config['name']}: validation F1={event['validation_metrics']['f1']:.6f}", flush=True)
        else:
            print(f"{config['name']}: {status['status']}; see log", flush=True)
        events.append(event)
        write_json(reports / "trials.json", events)
    write_json(reports / "trials.json", events)
    if not completed:
        raise RuntimeError("No completed trials; inspect logs")
    selected = max(completed, key=lambda r: (r["validation_metrics"]["f1"], r["validation_metrics"]["precision"],
                                            -r["validation_predict_seconds"]))
    write_json(reports / "selection.json", {"winner": selected["name"], "reason": "Validation F1, precision, prediction time",
                                            "test_used_for_selection": False, "trials": events})
    print(f"Locked winner: {selected['name']}. Evaluating untouched test.", flush=True)
    # The same deadline includes test evaluation; run it in a terminable process.
    status = run_child([sys.executable, "-m", "daleel_ml.experiment", "--evaluate", "--work", str(work),
                        "--reports", str(reports), "--artifact", str(artifact)],
                       reports / "test_evaluation.log", max(1, deadline - time.monotonic()))
    write_json(reports / "budget.json", {"budget_seconds": hours * 3600,
        "elapsed_seconds": time.monotonic() - start, "evaluation": status, "trials": events})
    if status["status"] != "completed":
        raise RuntimeError(f"Final evaluation {status['status']}; see test_evaluation.log")
    return json.loads((reports / "evaluation.json").read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="ml_data/raw/transactions/Fraud.csv")
    parser.add_argument("--work", default="ml_data/work/transactions")
    parser.add_argument("--reports", default="ml_data/reports/transactions")
    parser.add_argument("--artifact", default=str(DEFAULT_ARTIFACT_PATH))
    parser.add_argument("--hours", type=float, default=6.)
    parser.add_argument("--worker", type=int)
    parser.add_argument("--evaluate", action="store_true")
    args = parser.parse_args()
    if args.worker is not None:
        trial_worker(Path(args.work), args.worker)
    elif args.evaluate:
        selection = json.loads((Path(args.reports) / "selection.json").read_text(encoding="utf-8"))
        complete = [t for t in selection["trials"] if t["status"] == "completed"]
        selected = next(t for t in complete if t["name"] == selection["winner"])
        evaluate_locked(Path(args.work), Path(args.reports), selected, complete, Path(args.artifact))
    else:
        if args.hours <= 0:
            parser.error("--hours must be positive")
        run_experiment(args.data, args.work, args.reports, args.artifact, args.hours)


if __name__ == "__main__":
    main()
