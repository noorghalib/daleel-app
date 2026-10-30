"""Download a small, reproducible sparse sample from the Hugging Face CSV."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd
import requests


DATASET_REVISION = "0af54eb7e70f642a416d4022b03a2cb360efbd69"
DATASET_URL = (
    "https://huggingface.co/datasets/RegularGuyOIO/financial-transaction-fraud-dataset"
    f"/resolve/{DATASET_REVISION}/Fraud.csv"
)
DATASET_ID = "RegularGuyOIO/financial-transaction-fraud-dataset"
ROWS_API = "https://datasets-server.huggingface.co/rows"
COLUMNS = [
    "step",
    "type",
    "amount",
    "nameOrig",
    "oldbalanceOrg",
    "newbalanceOrig",
    "nameDest",
    "oldbalanceDest",
    "newbalanceDest",
    "isFraud",
    "isFlaggedFraud",
]


def _download_block(block_number: int, start: int, block_bytes: int, file_size: int) -> pd.DataFrame:
    end = min(start + block_bytes - 1, file_size - 1)
    response = requests.get(
        DATASET_URL,
        headers={"Range": f"bytes={start}-{end}"},
        timeout=60,
    )
    response.raise_for_status()
    if response.status_code != 206:
        raise RuntimeError(
            f"Expected an HTTP range response (206), got {response.status_code}."
        )

    payload = response.content
    if start == 0:
        header_end = payload.find(b"\n")
        if header_end < 0:
            raise RuntimeError("The first sample block did not contain the CSV header.")
        payload = payload[header_end + 1 :]
    else:
        first_line_end = payload.find(b"\n")
        if first_line_end < 0:
            return pd.DataFrame(columns=COLUMNS + ["_sample_block"])
        payload = payload[first_line_end + 1 :]

    # Drop the last partial line so CSV parsing never fabricates a broken row.
    last_line_end = payload.rfind(b"\n")
    if last_line_end < 0:
        return pd.DataFrame(columns=COLUMNS + ["_sample_block"])
    payload = payload[: last_line_end + 1]

    block = pd.read_csv(BytesIO(payload), header=None, names=COLUMNS)
    block["_sample_block"] = block_number
    return block


def download_sparse_sample(
    output_path: str | Path,
    blocks: int = 8,
    block_bytes: int = 131_072,
    workers: int = 4,
) -> pd.DataFrame:
    """Sample evenly spaced byte windows without downloading the full 493 MB CSV."""
    if blocks < 1 or block_bytes < 1024:
        raise ValueError("blocks must be >= 1 and block_bytes must be >= 1024")

    head = requests.head(DATASET_URL, allow_redirects=True, timeout=60)
    head.raise_for_status()
    file_size = int(head.headers["Content-Length"])
    last_start = max(0, file_size - block_bytes)
    starts = np.linspace(0, last_start, num=blocks, dtype=np.int64).tolist()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(_download_block, index, int(start), block_bytes, file_size)
            for index, start in enumerate(starts)
        ]
        blocks_data = [future.result() for future in futures]

    sample = pd.concat(blocks_data, ignore_index=True)
    sample["sample_row_id"] = np.arange(1, len(sample) + 1, dtype=np.int64)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    sample.to_csv(output, index=False)
    return sample


def download_random_row_sample(
    output_path: str | Path,
    pages: int = 100,
    seed: int = 20261025,
    workers: int = 8,
) -> pd.DataFrame:
    """Fetch reproducible random pages from the full row range via the HF viewer.

    Each page contains at most 100 adjacent rows. Random page offsets avoid the
    severe time-window bias found in the sparse byte-window diagnostic sample.
    """
    if pages < 1:
        raise ValueError("pages must be >= 1")
    metadata = requests.get(
        ROWS_API,
        params={
            "dataset": DATASET_ID,
            "config": "default",
            "split": "train",
            "offset": 0,
            "length": 1,
        },
        timeout=60,
    )
    metadata.raise_for_status()
    total_rows = int(metadata.json()["num_rows_total"])
    rng = np.random.default_rng(seed)
    offsets = rng.choice(total_rows - 100 + 1, size=min(pages, total_rows - 99), replace=False)

    def fetch_page(offset: int) -> pd.DataFrame:
        response = requests.get(
            ROWS_API,
            params={
                "dataset": DATASET_ID,
                "config": "default",
                "split": "train",
                "offset": int(offset),
                "length": 100,
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        rows = [item["row"] for item in payload["rows"]]
        page = pd.DataFrame(rows)
        page["_source_row_index"] = [item["row_idx"] for item in payload["rows"]]
        return page

    with ThreadPoolExecutor(max_workers=workers) as executor:
        pages_data = list(executor.map(fetch_page, offsets.tolist()))

    sample = pd.concat(pages_data, ignore_index=True)
    sample = sample.drop_duplicates(subset="_source_row_index").sort_values(
        "_source_row_index", kind="stable"
    )
    sample["sample_row_id"] = np.arange(1, len(sample) + 1, dtype=np.int64)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    sample.to_csv(output, index=False)
    return sample


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=["random", "sparse"], default="random")
    parser.add_argument("--pages", type=int, default=100, help="Random viewer pages (up to 100 rows each).")
    parser.add_argument("--seed", type=int, default=20261025)
    parser.add_argument("--blocks", type=int, default=8)
    parser.add_argument("--block-bytes", type=int, default=131_072)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--output",
        default="ml_data/samples/fraud_preview.csv",
        help="Output CSV path for the sampled rows.",
    )
    args = parser.parse_args()
    if args.method == "random":
        sample = download_random_row_sample(
            args.output, pages=args.pages, seed=args.seed, workers=args.workers
        )
    else:
        sample = download_sparse_sample(args.output, args.blocks, args.block_bytes, args.workers)
    print(f"Saved {len(sample):,} sampled rows to {args.output}")
    print("Class counts:", sample["isFraud"].value_counts(dropna=False).to_dict())
    print("Missing values:", sample.isna().sum().to_dict())
    print("Duplicate complete rows:", int(sample.duplicated(subset=COLUMNS).sum()))


if __name__ == "__main__":
    main()
