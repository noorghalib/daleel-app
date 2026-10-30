"""Strict CSV adapter; rejects invalid rows instead of silently dropping them."""
import csv
import io

from service.app import InputError, LIMITS, TX, HEADERS, LINES, ACCOUNTS, number, string


def read_ledger_table(upload, table):
    """Keep identifiers as strings; only amounts and line numbers become numeric."""
    fields = {"headers": HEADERS, "lines": LINES, "chart_of_accounts": ACCOUNTS}[table]
    if not upload.filename or not upload.filename.lower().endswith(".csv"):
        raise InputError(f"{table}: choose a .csv file")
    try:
        reader = csv.reader(io.StringIO(upload.read().decode("utf-8-sig")), strict=True)
        names = next(reader, None)
        if names is None:
            raise InputError(f"{table}: CSV must contain a header row")
        names = [name.strip() for name in names]
        if len(names) != len(set(names)) or set(names) != set(fields):
            raise InputError(f"{table}: required columns are {', '.join(fields)}; no extra or duplicate columns")
        records = []
        for row in reader:
            if len(records) >= LIMITS[table]:
                raise InputError(f"{table}: exceeds {LIMITS[table]} rows", "batch_too_large", 413)
            location = f"{table}, CSV line {reader.line_num}"
            if len(row) != len(names):
                raise InputError(f"{location}: expected {len(names)} values, found {len(row)}")
            record = dict(zip(names, (value.strip() for value in row)))
            for field in ("document_date", "auxiliary_account_number"):
                if field in record and record[field] == "":
                    record[field] = None
            if table == "lines":
                for field in ("line_number", "debit_amount", "credit_amount", "local_amount"):
                    raw = record[field]
                    try:
                        record[field] = None if raw == "" and field != "line_number" else float(raw)
                        number(record[field], field, nullable=field != "line_number", integer=field == "line_number")
                    except (ValueError, InputError):
                        raise InputError(f"{location}: {field} must be a finite number; line_number must be a nonnegative integer") from None
                record["line_number"] = int(record["line_number"])
            records.append(record)
        if table == "headers" and not records:
            raise InputError("headers: at least one journal entry is required")
        return records
    except UnicodeDecodeError:
        raise InputError(f"{table}: save the file as CSV UTF-8") from None
    except csv.Error:
        raise InputError(f"{table}: malformed CSV; check quotes and separators") from None


def read_transactions(upload):
    if not upload.filename or not upload.filename.lower().endswith(".csv"):
        raise InputError("Choose a .csv file")
    try:
        reader = csv.reader(io.StringIO(upload.read().decode("utf-8-sig")), strict=True)
        headers = next(reader, None)
        if not headers:
            raise InputError("The CSV is empty")
        headers = [value.strip() for value in headers]
        if len(headers) != len(set(headers)):
            raise InputError("CSV column names must not be duplicated")
        missing = set(TX[1:]) - set(headers)
        extra = set(headers) - set(TX)
        if missing or extra:
            raise InputError(f"Missing columns: {', '.join(sorted(missing)) or '-'}; unsupported columns: {', '.join(sorted(extra)) or '-'}")
        records = []
        for row in reader:
            if len(records) >= LIMITS["transactions"]:
                raise InputError("CSV exceeds 10,000 transactions", "batch_too_large", 413)
            location = f"CSV line {reader.line_num}"
            if len(row) != len(headers):
                raise InputError(f"{location}: expected {len(headers)} values, found {len(row)}")
            record = dict(zip(headers, (value.strip() for value in row)))
            record.setdefault("record_id", f"row-{len(records) + 1}")
            try:
                string(record["record_id"], "record_id")
                string(record["type"], "type")
                for field in ["step", *TX[3:]]:
                    raw = record[field]
                    if not raw and field != "step":
                        record[field] = None
                    else:
                        try:
                            record[field] = float(raw)
                        except ValueError:
                            raise InputError(f"{field} must be numeric; use an empty cell for a missing amount") from None
                    number(record[field], field, nullable=field != "step", integer=field == "step")
                record["step"] = int(record["step"])
            except InputError as exc:
                raise InputError(f"{location}: {exc}") from exc
            records.append(record)
        if not records:
            raise InputError("The CSV has headers but no transactions")
        return records
    except UnicodeDecodeError:
        raise InputError("Save the file as CSV UTF-8 and try again") from None
    except csv.Error:
        raise InputError("Malformed CSV: check quotes and separators") from None
