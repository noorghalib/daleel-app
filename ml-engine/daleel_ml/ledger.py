"""Unsupervised, document-level ForensicBench models; no fraud labels are inferred."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import argparse
import json
import math
import time

import joblib
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import IsolationForest
from sklearn.impute import SimpleImputer
from threadpoolctl import threadpool_limits

from .download import SECTORS, SOURCES, sha256
from .experiment import write_json, versions, run_child
from .model import temporal_split, RANDOM_STATE

DEFAULT_LEDGER_ARTIFACT = Path(__file__).parent / "artifacts" / "ledger_detector.joblib"
CATEGORICAL = ["document_type", "source", "business_process", "account_pattern"]
NUMERIC = ["total_debit", "total_credit", "net_local", "line_count", "max_line_amount",
           "mean_line_amount", "unique_accounts", "unique_parties", "balance_gap", "relative_balance_gap",
           "round_amount", "posting_lag_days", "posting_weekday", "posting_month", "missing_lines",
           "unknown_account_ratio", "asset_ratio", "liability_ratio", "equity_ratio", "tiers_ratio",
           "expense_ratio", "revenue_ratio", "special_ratio", "duplicate_line_key_count"]
OUTPUT_COLUMNS = ["document_id", "sector", "anomaly_score", "review_alert", "rule_alert", "rule_reasons"]


def aggregate_ledger(headers, lines, chart_of_accounts):
    required_h = {"document_id", "posting_date", "document_date", "document_type", "source", "business_process"}
    required_l = {"document_id", "line_number", "gl_account", "debit_amount", "credit_amount", "local_amount", "auxiliary_account_number"}
    for label, frame, required in (("headers", headers, required_h), ("lines", lines, required_l),
                                    ("chart_of_accounts", chart_of_accounts, {"account_number", "account_type"})):
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"Missing {label} columns: {', '.join(sorted(missing))}")
    if headers.document_id.isna().any() or headers.document_id.duplicated().any():
        raise ValueError("Header document_id must be non-null and unique within each sector")
    if lines[["document_id", "line_number"]].isna().any().any():
        raise ValueError("Line document/line keys must be non-null")
    if not lines.document_id.isin(headers.document_id).all():
        raise ValueError("Orphan journal lines reference unknown headers")
    if chart_of_accounts.account_number.duplicated().any():
        raise ValueError("Chart of accounts must have unique account_number")
    detail = lines[list(required_l)].copy()
    # The pinned source contains repeated line keys with different amounts.
    # Keep every line and expose the repetition as evidence, never deduplicate it.
    detail["duplicate_line_key"] = lines.duplicated(["document_id", "line_number"], keep=False).astype(int)
    invalid = pd.Series(False, index=detail.index)
    for col in ("debit_amount", "credit_amount", "local_amount"):
        detail[col] = pd.to_numeric(detail[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
        invalid |= detail[col].isna()
    detail["invalid_amount"] = invalid.astype(int)
    detail["negative_dc"] = ((detail.debit_amount < 0) | (detail.credit_amount < 0)).astype(int)
    detail["line_amount"] = detail.debit_amount.abs() + detail.credit_amount.abs()
    account_types = chart_of_accounts.set_index("account_number").account_type
    detail["account_type"] = detail.gl_account.map(account_types)
    detail["unknown_account"] = detail.account_type.isna().astype(float)
    for category in ("Asset", "Liability", "Equity", "Tiers", "Expense", "Revenue", "Special"):
        detail[category.lower() + "_ratio"] = detail.account_type.eq(category).astype(float)
    grouped = detail.groupby("document_id", sort=False)
    values = grouped.agg(total_debit=("debit_amount", "sum"), total_credit=("credit_amount", "sum"),
        net_local=("local_amount", "sum"), line_count=("line_number", "size"),
        max_line_amount=("line_amount", "max"), mean_line_amount=("line_amount", "mean"),
        unique_accounts=("gl_account", "nunique"), unique_parties=("auxiliary_account_number", "nunique"),
        invalid_amount=("invalid_amount", "max"), negative_dc=("negative_dc", "max"),
        unknown_account_ratio=("unknown_account", "mean"),
        duplicate_line_key_count=("duplicate_line_key", "sum"))
    for category in ("asset", "liability", "equity", "tiers", "expense", "revenue", "special"):
        values[category + "_ratio"] = grouped[category + "_ratio"].mean()
    # Account codes describe accounting structure; personal and document identifiers are excluded.
    values["account_pattern"] = grouped.gl_account.agg(lambda s: "|".join(sorted(set(s.dropna().astype(str)))))
    result = headers.reset_index(drop=True).merge(values.reset_index(), on="document_id", how="left", validate="one_to_one", sort=False)
    result["missing_lines"] = result.line_count.isna().astype(float)
    result["balance_gap"] = (result.total_debit - result.total_credit).abs()
    result["relative_balance_gap"] = result.balance_gap / result.total_debit.abs().clip(lower=.01)
    result["round_amount"] = (result.total_debit.mod(100) == 0).astype(float)
    posting = pd.to_datetime(result.posting_date, errors="coerce")
    document = pd.to_datetime(result.document_date, errors="coerce")
    if posting.isna().any():
        raise ValueError("Every header must have a valid posting_date")
    result["posting_lag_days"] = (posting - document).dt.days
    result["posting_weekday"], result["posting_month"] = posting.dt.dayofweek, posting.dt.month
    reasons = np.full(len(result), "", dtype=object)
    checks = [(result.missing_lines == 1, "journal entry has no lines"),
              (result.balance_gap > .02, "debits and credits do not balance"),
              (result.negative_dc == 1, "negative debit or credit"),
              (result.invalid_amount == 1, "invalid line amount"),
              (result.unknown_account_ratio > 0, "account missing from chart of accounts"),
              (result.duplicate_line_key_count > 0, "duplicate document/line key; all source lines retained"),
              (document.isna(), "invalid document_date")]
    for mask, message in checks:
        idx = np.flatnonzero(mask.fillna(False).to_numpy())
        reasons[idx] = [f"{s}; {message}" if s else message for s in reasons[idx]]
    result["rule_reasons"] = reasons
    return result


class LedgerEncoder:
    """Frequency encodings and imputation are learned strictly from training entries."""
    def fit(self, frame):
        self.frequencies = {col: frame[col].fillna("__MISSING__").astype(str).value_counts(normalize=True).to_dict()
                            for col in CATEGORICAL}
        self.imputer = SimpleImputer(strategy="median", keep_empty_features=True)
        self.imputer.fit(self._raw(frame))
        return self

    def _raw(self, frame):
        features = frame[NUMERIC].copy()
        for column in CATEGORICAL:
            features[column + "_frequency"] = frame[column].fillna("__MISSING__").astype(str).map(self.frequencies[column]).fillna(0.)
        return features.replace([np.inf, -np.inf], np.nan).astype("float32")

    def transform(self, frame):
        return self.imputer.transform(self._raw(frame)).astype("float32")


def review_mask(scores, ids, fraction=.01):
    if not 0 < fraction <= 1:
        raise ValueError("review_fraction must be in (0, 1]")
    flags = np.zeros(len(scores), dtype=bool)
    if len(scores):
        order = np.lexsort((np.asarray(ids, dtype=str), -np.asarray(scores)))
        flags[order[:math.ceil(len(scores) * fraction)]] = True
    return flags


class DaleelLedgerModel:
    def __init__(self, payload):
        self.models, self.metadata = payload["models"], payload["metadata"]

    def predict(self, headers, lines, chart_of_accounts, sector, review_fraction=.01):
        if sector not in self.models:
            raise ValueError(f"Unknown sector: {sector}; choose one of {sorted(self.models)}")
        frame = aggregate_ledger(headers, lines, chart_of_accounts)
        if frame.empty:
            return pd.DataFrame(columns=OUTPUT_COLUMNS)
        entry = self.models[sector]
        with threadpool_limits(limits=8):
            scores = -entry["model"].score_samples(entry["encoder"].transform(frame))
        flags = scores >= entry["threshold"] if review_fraction is None else review_mask(scores, frame.document_id, review_fraction)
        return pd.DataFrame({"document_id": frame.document_id.to_numpy(), "sector": sector,
            "anomaly_score": scores, "review_alert": flags,
            "rule_alert": frame.rule_reasons.ne("").to_numpy(), "rule_reasons": frame.rule_reasons.to_numpy()}, columns=OUTPUT_COLUMNS)


def load_ledger_model(path=DEFAULT_LEDGER_ARTIFACT):
    payload = joblib.load(path)
    if not isinstance(payload, dict) or not {"models", "metadata"}.issubset(payload):
        raise ValueError("Invalid ledger artifact")
    return DaleelLedgerModel(payload)


@lru_cache(maxsize=4)
def _cached_ledger(path):
    return load_ledger_model(path)


def predict_ledger(headers, lines, chart_of_accounts, sector, model_path=DEFAULT_LEDGER_ARTIFACT, review_fraction=.01):
    return _cached_ledger(str(Path(model_path).resolve())).predict(headers, lines, chart_of_accounts, sector, review_fraction)


def sector_worker(root, sector, destination):
    begin = time.monotonic()
    source = root / sector
    headers = pd.read_parquet(source / "je_header.parquet")
    lines = pd.read_parquet(source / "je_line.parquet")
    accounts = pd.read_parquet(source / "chart_of_accounts.parquet")
    frame = aggregate_ledger(headers, lines, accounts)
    source_counts = {"headers": len(headers), "lines": len(lines), "chart_of_accounts": len(accounts),
                     "duplicate_line_keys_extra_rows": int(lines.duplicated(["document_id", "line_number"]).sum())}
    del headers, lines, accounts
    steps = pd.to_datetime(frame.posting_date).astype("int64")
    train, validation, test = temporal_split(pd.DataFrame({"step": steps}))
    encoder = LedgerEncoder().fit(frame.iloc[train])
    with threadpool_limits(limits=8):
        model = IsolationForest(n_estimators=300, max_samples=4096, n_jobs=8, random_state=RANDOM_STATE).fit(encoder.transform(frame.iloc[train]))
        validation_scores = -model.score_samples(encoder.transform(frame.iloc[validation]))
        test_matrix = encoder.transform(frame.iloc[test])
        test_scores = -model.score_samples(test_matrix)
        # Independent seed measures ranking stability, not fraud-detection accuracy.
        repeat = IsolationForest(n_estimators=300, max_samples=4096, n_jobs=8, random_state=RANDOM_STATE + 1).fit(encoder.transform(frame.iloc[train]))
        repeat_scores = -repeat.score_samples(test_matrix)
    ids = frame.iloc[test].document_id.to_numpy()
    first_flags = review_mask(test_scores, ids)
    second_flags = review_mask(repeat_scores, ids)
    intersection = int((first_flags & second_flags).sum())
    union = int((first_flags | second_flags).sum())
    correlation = float(spearmanr(test_scores, repeat_scores).statistic)
    report = {"sector": sector, "source_counts": source_counts,
              "train_rows": len(train), "validation_rows": len(validation), "test_rows": len(test),
              "train_last_date": str(frame.iloc[train].posting_date.max()),
              "validation_first_date": str(frame.iloc[validation].posting_date.min()),
              "validation_last_date": str(frame.iloc[validation].posting_date.max()),
              "test_first_date": str(frame.iloc[test].posting_date.min()),
              "review_fraction": .01, "test_review_alerts": int(first_flags.sum()),
              "test_rule_alerts": int(frame.iloc[test].rule_reasons.ne("").sum()),
              "rank_spearman_across_seeds": correlation if np.isfinite(correlation) else None,
              "top_1_percent_jaccard_across_seeds": intersection / union if union else 1.,
              "test_score_quantiles": {str(q): float(np.quantile(test_scores, q)) for q in (0, .5, .9, .95, .99, 1)},
              "elapsed_seconds": time.monotonic() - begin,
              "evaluation_note": "Unlabelled data: no fraud precision, recall or F1 can be calculated"}
    threshold = float(np.quantile(validation_scores, .99))
    payload = {"model": model, "encoder": encoder, "threshold": threshold, "report": report}
    destination.mkdir(parents=True, exist_ok=True)
    joblib.dump(payload, destination / f"{sector}.joblib", compress=3)
    write_json(destination / f"{sector}.json", report)
    audit = frame.iloc[test][["document_id", "rule_reasons"]].copy()
    audit["sector"], audit["anomaly_score"], audit["review_alert"] = sector, test_scores, first_flags
    audit.sort_values(["anomaly_score", "document_id"], ascending=[False, True], kind="stable").head(100).to_csv(destination / f"{sector}_top_review.csv", index=False)
    print(json.dumps(report, indent=2), flush=True)


def train_ledgers(root="ml_data/raw/forensic", reports="ml_data/reports/ledger", artifact=DEFAULT_LEDGER_ARTIFACT, hours=2.):
    import sys
    root, reports, artifact = Path(root), Path(reports), Path(artifact)
    reports.mkdir(parents=True, exist_ok=True)
    work = reports.parent.parent / "work" / "ledger"
    work.mkdir(parents=True, exist_ok=True)
    begin = time.monotonic()
    events, models, results = [], {}, []
    for sector in SECTORS:
        remaining = hours * 3600 - (time.monotonic() - begin)
        if remaining < 10:
            events.append({"sector": sector, "status": "skipped_budget"})
            continue
        print(f"Training ledger sector: {sector}", flush=True)
        status = run_child([sys.executable, "-m", "daleel_ml.ledger", "--sector-worker", sector,
            "--root", str(root), "--reports", str(work)], reports / f"{sector}.log", max(1, remaining - 5))
        events.append({"sector": sector, **status})
        write_json(reports / "trials.json", events)
        if status["status"] != "completed":
            continue
        payload = joblib.load(work / f"{sector}.joblib")
        results.append(payload.pop("report"))
        models[sector] = payload
        (reports / f"{sector}_top_review.csv").write_bytes((work / f"{sector}_top_review.csv").read_bytes())
    write_json(reports / "trials.json", events)
    if set(models) != set(SECTORS):
        raise RuntimeError("Not all five sectors completed; inspect ledger/trials.json and logs")
    metadata = {"task": "unsupervised document anomaly ranking", "source_dataset": SOURCES["forensic"][0],
                "revision": SOURCES["forensic"][1], "sectors": SECTORS, "random_state": RANDOM_STATE,
                "numeric_features": NUMERIC, "frequency_features": CATEGORICAL,
                "review_policy": "Top ceil(n * review_fraction) per sector batch; default .01; None uses validation threshold",
                "score_semantics": "Negative IsolationForest score_samples; higher means more unusual, not fraud probability",
                "versions": versions(), "labels_available": False}
    artifact.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"models": models, "metadata": metadata}, artifact, compress=3)
    write_json(artifact.with_suffix(".metadata.json"), metadata)
    report = {"sectors": results, "trials": events, "metadata": metadata, "budget_seconds": hours * 3600,
              "elapsed_seconds": time.monotonic() - begin, "artifact_bytes": artifact.stat().st_size,
              "artifact_sha256": sha256(artifact)}
    write_json(reports / "evaluation.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="ml_data/raw/forensic")
    parser.add_argument("--reports", default="ml_data/reports/ledger")
    parser.add_argument("--artifact", default=str(DEFAULT_LEDGER_ARTIFACT))
    parser.add_argument("--hours", type=float, default=2.)
    parser.add_argument("--sector-worker", choices=SECTORS)
    args = parser.parse_args()
    if args.hours <= 0:
        parser.error("--hours must be positive")
    if args.sector_worker:
        sector_worker(Path(args.root), args.sector_worker, Path(args.reports))
    else:
        train_ledgers(args.root, args.reports, args.artifact, args.hours)


if __name__ == "__main__":
    # Persist custom encoders under their importable module, never __main__.
    from daleel_ml.ledger import main as entrypoint
    entrypoint()
