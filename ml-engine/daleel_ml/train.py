"""Command line entry point for training/evaluating the standalone fraud model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .model import DEFAULT_ARTIFACT_PATH, DEFAULT_REPORT_DIR, train_and_evaluate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data",
        default="ml_data/samples/financial_fraud_sample.csv",
        help="CSV with the published transaction schema and isFraud target.",
    )
    parser.add_argument("--artifact", default=str(DEFAULT_ARTIFACT_PATH))
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Optional prefix limit for a quick resource-safe smoke run.",
    )
    args = parser.parse_args()

    source = Path(args.data)
    if not source.exists():
        raise FileNotFoundError(
            f"Input CSV not found: {source}. Download a sample with `python -m daleel_ml.sample_data`."
        )
    data = pd.read_csv(source, nrows=args.max_rows)
    result = train_and_evaluate(data, artifact_path=args.artifact, report_dir=args.report_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
