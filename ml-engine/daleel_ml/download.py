"""Pinned, resumable dataset downloads. Raw data stays outside release archives."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

import requests

SOURCES = {
    "transactions": ("RegularGuyOIO/financial-transaction-fraud-dataset", "0af54eb7e70f642a416d4022b03a2cb360efbd69"),
    "forensic": ("WaguyMZ/ForensicBench", "9c259ac178a9aeddf4a1ea509cf526bbccb5c04a"),
}
SECTORS = ["energy", "healthcare", "luxurygoods", "manufacturing", "transport"]


def sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_file(url, destination, expected_size, expected_sha256=None, retries=4):
    """Resume only validated HTTP byte ranges; a server ignoring Range restarts safely."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    if destination.exists() and destination.stat().st_size == expected_size:
        digest = sha256(destination)
        if expected_sha256 is None or digest == expected_sha256:
            return digest
    error = None
    for attempt in range(retries):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > expected_size:
                partial.unlink()
                offset = 0
            if offset != expected_size:
                headers = {"Accept-Encoding": "identity"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                with requests.get(url, headers=headers, stream=True, timeout=(30, 90)) as response:
                    response.raise_for_status()
                    if response.status_code == 206:
                        if not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                            raise ValueError("Server returned an invalid Content-Range")
                    elif response.status_code == 200:
                        offset = 0
                    else:
                        raise ValueError(f"Unexpected HTTP status: {response.status_code}")
                    with partial.open("ab" if offset else "wb") as stream:
                        for block in response.iter_content(1024 * 1024):
                            if block:
                                stream.write(block)
            if partial.stat().st_size != expected_size:
                raise ValueError(f"Incomplete file: {partial.stat().st_size}/{expected_size}")
            digest = sha256(partial)
            if expected_sha256 and digest != expected_sha256:
                partial.unlink()
                raise ValueError("SHA256 mismatch; corrupt partial removed")
            partial.replace(destination)
            return digest
        except (requests.RequestException, OSError, ValueError) as exc:
            error = exc
            if attempt + 1 < retries:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Download failed after {retries} attempts: {url}: {error}") from error


def download_all(root="ml_data/raw", datasets=("transactions", "forensic")):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root.parent / "dataset_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    for name in datasets:
        repo, revision = SOURCES[name]
        response = requests.get(f"https://huggingface.co/api/datasets/{repo}/tree/{revision}",
                                params={"recursive": "true", "limit": 1000}, timeout=60)
        response.raise_for_status()
        records = []
        for item in response.json():
            remote = item["path"]
            if item["type"] != "file" or not remote.endswith((".csv", ".parquet")):
                continue
            relative = remote.removeprefix("data/")
            target = root / name / relative
            print(f"Downloading/verifying {name}/{relative} ({item['size']:,} bytes)", flush=True)
            digest = download_file(f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{remote}",
                                   target, item["size"], item.get("lfs", {}).get("oid"))
            if target.suffix == ".parquet":
                import pyarrow.parquet as pq
                rows = pq.ParquetFile(target).metadata.num_rows
            else:
                import pandas as pd
                rows = sum(len(chunk) for chunk in pd.read_csv(target, usecols=[0], chunksize=250_000))
            records.append({"path": f"{name}/{relative}", "remote_path": remote,
                            "bytes": item["size"], "sha256": digest, "rows": rows})
        if not records:
            raise RuntimeError(f"No data files found for {repo}")
        manifest[name] = {"dataset": repo, "revision": revision, "files": records}
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="ml_data/raw")
    parser.add_argument("--dataset", choices=["all", *SOURCES], default="all")
    args = parser.parse_args()
    download_all(args.root, tuple(SOURCES) if args.dataset == "all" else (args.dataset,))


if __name__ == "__main__":
    main()
