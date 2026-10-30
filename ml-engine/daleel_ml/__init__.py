"""Standalone supervised transaction-fraud model for the Daleel AI prototype."""

from .model import FEATURE_COLUMNS, TARGET_COLUMN, load_model, predict

def load_ledger_model(path=None):
    from .ledger import load_ledger_model as load
    return load() if path is None else load(path)


def predict_ledger(headers, lines, chart_of_accounts, sector, model_path=None, review_fraction=.01):
    from .ledger import predict_ledger as run
    from .ledger import DEFAULT_LEDGER_ARTIFACT
    return run(headers, lines, chart_of_accounts, sector,
               DEFAULT_LEDGER_ARTIFACT if model_path is None else model_path, review_fraction)


__all__ = ["FEATURE_COLUMNS", "TARGET_COLUMN", "load_model", "predict", "load_ledger_model", "predict_ledger"]
