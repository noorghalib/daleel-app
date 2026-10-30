"""Leakage-safe feature preparation for the public transaction fraud dataset."""

from __future__ import annotations

import numpy as np
import pandas as pd


TARGET_COLUMN = "isFraud"
LEAKAGE_AND_IDENTIFIER_COLUMNS = {
    "isFraud",          # target label
    "isFlaggedFraud",   # existing fraud flag; direct target leakage
    "nameOrig",         # account identifier
    "nameDest",         # account identifier
    "record_id",        # caller-provided report identifier
    "sample_row_id",    # sampler/report metadata
    "_sample_block",    # sampler/report metadata
}

NUMERIC_FEATURES = [
    "step",
    "amount",
    "oldbalanceOrg",
    "newbalanceOrig",
    "oldbalanceDest",
    "newbalanceDest",
    "origin_balance_gap",
    "destination_balance_gap",
    "origin_balance_check_applicable",
    "destination_balance_check_applicable",
    "amount_to_origin_balance_ratio",
]
CATEGORICAL_FEATURES = ["type"]
FEATURE_COLUMNS = NUMERIC_FEATURES + CATEGORICAL_FEATURES
REQUIRED_INPUT_COLUMNS = [
    "step",
    "type",
    "amount",
    "oldbalanceOrg",
    "newbalanceOrig",
    "oldbalanceDest",
    "newbalanceDest",
]

DEBIT_TYPES = {"PAYMENT", "TRANSFER", "CASH_OUT", "DEBIT"}


def build_features(data: pd.DataFrame) -> pd.DataFrame:
    """Build model features without labels, account IDs, or existing flags.

    The account identifiers are intentionally excluded. Derived balance gaps
    are retained as numeric signals and are not assertions of fraud.
    """
    missing = [column for column in REQUIRED_INPUT_COLUMNS if column not in data.columns]
    if missing:
        raise ValueError("Missing required transaction columns: " + ", ".join(missing))

    features = pd.DataFrame(index=data.index)
    numeric_source = [
        "step",
        "amount",
        "oldbalanceOrg",
        "newbalanceOrig",
        "oldbalanceDest",
        "newbalanceDest",
    ]
    for column in numeric_source:
        values = pd.to_numeric(data[column], errors="coerce")
        features[column] = values.replace([np.inf, -np.inf], np.nan)

    transaction_type = data["type"].astype("string").str.upper().fillna("__MISSING__")
    debit_mask = transaction_type.isin(DEBIT_TYPES)
    transfer_mask = transaction_type.eq("TRANSFER")

    expected_origin = (features["oldbalanceOrg"] - features["amount"]).clip(lower=0)
    features["origin_balance_gap"] = (
        features["newbalanceOrig"] - expected_origin
    ).where(debit_mask)
    features["origin_balance_check_applicable"] = debit_mask.astype(float)

    expected_destination = features["oldbalanceDest"] + features["amount"]
    features["destination_balance_gap"] = (
        features["newbalanceDest"] - expected_destination
    ).where(transfer_mask)
    features["destination_balance_check_applicable"] = transfer_mask.astype(float)

    denominator = features["oldbalanceOrg"].abs().replace(0, np.nan)
    features["amount_to_origin_balance_ratio"] = features["amount"] / denominator
    features["type"] = transaction_type

    # Keep this explicit allowlist as a guard against accidental label leakage.
    return features[FEATURE_COLUMNS].copy()
