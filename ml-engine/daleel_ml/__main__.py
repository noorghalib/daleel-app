"""User-facing download, training, evaluation and batch prediction commands."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import pandas as pd


def table(path):
    path = Path(path)
    return pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path,
        dtype={"document_id": "string", "gl_account": "string", "account_number": "string"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("download")
    download.add_argument("--dataset", choices=["all", "transactions", "forensic"], default="all")
    download.add_argument("--root", default="ml_data/raw")
    train = commands.add_parser("train")
    train.add_argument("task", choices=["transactions", "ledger"])
    train.add_argument("--data", help="Transaction CSV or ForensicBench root directory")
    train.add_argument("--hours", type=float)
    evaluate = commands.add_parser("evaluate", help="Display the recorded independent evaluation; does not retrain or retune")
    evaluate.add_argument("task", choices=["transactions", "ledger"])
    evaluate.add_argument("--reports", default="ml_data/reports")
    predict = commands.add_parser("predict")
    predict.add_argument("task", choices=["transactions", "ledger"])
    predict.add_argument("--input", help="Transaction CSV")
    predict.add_argument("--output", required=True)
    predict.add_argument("--model")
    predict.add_argument("--headers")
    predict.add_argument("--lines")
    predict.add_argument("--accounts")
    predict.add_argument("--sector")
    predict.add_argument("--review-fraction", type=float, default=.01)
    args = parser.parse_args()
    if args.command == "download":
        from .download import download_all, SOURCES
        download_all(args.root, tuple(SOURCES) if args.dataset == "all" else (args.dataset,))
    elif args.command == "train":
        if args.hours is not None and args.hours <= 0:
            parser.error("--hours must be positive")
        if args.task == "transactions":
            from .experiment import run_experiment
            from .model import DEFAULT_ARTIFACT_PATH
            run_experiment(args.data or "ml_data/raw/transactions/Fraud.csv", "ml_data/work/transactions",
                           "ml_data/reports/transactions", DEFAULT_ARTIFACT_PATH, args.hours or 6.)
        else:
            from .ledger import train_ledgers
            train_ledgers(root=args.data or "ml_data/raw/forensic", hours=args.hours or 2.)
    elif args.command == "evaluate":
        report = Path(args.reports) / args.task / "evaluation.json"
        print(json.dumps(json.loads(report.read_text(encoding="utf-8")), ensure_ascii=False, indent=2))
    elif args.task == "transactions":
        if not args.input:
            parser.error("Transaction prediction requires --input")
        from .model import load_model, PREDICTION_COLUMNS
        model = load_model(args.model) if args.model else load_model()
        output = Path(args.output)
        if output.resolve() == Path(args.input).resolve():
            parser.error("Output must not overwrite the input dataset")
        output.parent.mkdir(parents=True, exist_ok=True)
        offset, first = 0, True
        for chunk in pd.read_csv(args.input, chunksize=100_000):
            if "record_id" not in chunk and "sample_row_id" not in chunk:
                chunk["record_id"] = range(offset, offset + len(chunk))
            result = model.predict(chunk)
            result.to_csv(output, mode="w" if first else "a", header=first, index=False, encoding="utf-8-sig" if first else "utf-8")
            offset += len(chunk)
            first = False
        if first:
            pd.DataFrame(columns=PREDICTION_COLUMNS).to_csv(output, index=False)
        print(f"Saved {offset:,} transaction predictions to {output}")
    else:
        if not all([args.headers, args.lines, args.accounts, args.sector]):
            parser.error("Ledger prediction requires --headers, --lines, --accounts, --sector")
        if Path(args.output).resolve() in {Path(p).resolve() for p in [args.headers, args.lines, args.accounts]}:
            parser.error("Output must not overwrite an input dataset")
        from .ledger import load_ledger_model
        model = load_ledger_model(args.model) if args.model else load_ledger_model()
        result = model.predict(table(args.headers), table(args.lines), table(args.accounts), args.sector, args.review_fraction)
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(args.output, index=False, encoding="utf-8-sig")
        print(f"Saved {len(result):,} journal-entry predictions to {args.output}")


if __name__ == "__main__":
    main()
