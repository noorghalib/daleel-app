# Daleel | دليل

Daleel is a financial review workspace that helps teams identify transactions and journal entries that deserve closer inspection. It combines an Arabic, right-to-left web interface, trained machine-learning models, accounting checks, and a PostgreSQL analysis history in one Flask application.

Upload a transaction CSV or a set of journal CSVs, inspect the results, and return to saved analyses without rerunning the models. The goal is to reduce manual screening and make review evidence easier to find. An alert indicates a need for investigation; it does not establish fraud.

## Features

- Arabic dashboard with search, filtering, transaction views, journal views, and a review queue.
- Transaction classification using a trained model and separate balance/amount validation rules.
- Sector-specific journal anomaly ranking and checks for unbalanced entries, unknown accounts, repeated line keys, missing amounts, and missing lines.
- PostgreSQL storage for predictions, model identifiers, analysis timestamps, and selected upload metadata.
- Password-based sign-in, hashed passwords, session cookies, CSRF checks, and a Bearer-token API for integrations.
- One server serves the frontend, API, and model inference. No external AI API or second inference server is required.

## Languages and technology

| Layer | Technology |
| --- | --- |
| Backend | Python 3.13, Flask, Waitress |
| Frontend | HTML5, CSS3, JavaScript; Arabic RTL layout |
| Database | PostgreSQL, SQLAlchemy, Flask-SQLAlchemy, psycopg |
| Schema migrations | Alembic and Flask-Migrate |
| Data processing | NumPy, pandas, PyArrow |
| Machine learning | scikit-learn, XGBoost for training experiments, joblib model serialization |
| Configuration and delivery | python-dotenv, Docker, GitHub Actions |
| Tests | Python unittest; real bundled models with isolated storage tests |

Use Python 3.13 for this release. The serialized models were created with the dependency versions in `ml-engine/requirements-lock.txt`; changing scikit-learn or NumPy versions without rebuilding and validating the models is unsupported.

## Algorithms and interpretation

| Component | Algorithm | Role |
| --- | --- | --- |
| Transaction inference | Histogram-based Gradient Boosting (`HistGradientBoostingClassifier`) | Selected deployed classifier |
| Transaction experiments | Random Forest; economic, deep, and regularized XGBoost variants; extended histogram boosting | Compared during training; these are not all used for each prediction |
| Journal inference | Isolation Forest | Separate anomaly-ranking models for five sectors |
| Validation rules | Deterministic checks | Independent evidence, not a machine-learning algorithm |

The transaction model uses transaction type, step, amount, balances, and derived balance features. Its saved decision threshold is approximately **0.99796135**. `fraud_score` is an uncalibrated model output, not a guaranteed probability of fraud.

Journal analysis supports `energy`, `healthcare`, `luxurygoods`, `manufacturing`, and `transport`. By default, it selects the highest-scoring `ceil(number of entries × 0.01)` entries in the submitted batch for review. A batch can therefore have a review alert even when its entries are legitimate. Rankings depend on the selected sector and batch. Journal scores are not fraud probabilities, and should not be compared directly with transaction scores.

The website displays either **Normal** or **Needs review** based on model or rule alerts. It does not currently assign a separate **High risk** category. Rule evidence remains distinguishable from model alerts in record details.

## Project structure

```text
app.py                    Flask application, uploads, and persistence
workspace_backend.py      Sign-in and dashboard data
csv_upload.py             Strict CSV parsing
deployment.py             Database and hosting configuration
frontend/                 HTML, styles, scripts, and licensed font assets
service/                  Prediction API and input validation
ml-engine/daleel_ml/       Model loading, features, training, and trained artifacts
ml-engine/examples/input/ Synthetic upload examples
api/openapi.json          Prediction API specification
migrations/               Database schema history
tests/                    API and deployment tests
test_backend_storage.py   Authentication, upload, and storage tests
Dockerfile                Container build
```

The trained artifacts are backend resources. They are not exposed as static website files. The application checks their SHA-256 hashes against `MODEL_IDENTITY.json` before loading them. Do not replace them with untrusted joblib files.

## Local setup

Install Python 3.13 and PostgreSQL. Create a database named `daleel` and a dedicated login role that owns it, such as `daleel_app`. The application role does not need PostgreSQL superuser privileges.

From the project directory in PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

Edit `.env` locally. Enter the database connection settings and set `DALEEL_API_TOKEN` to the generated random value. Do not commit `.env`, share the token, or place it in frontend code. The website account, PostgreSQL login, and API token are three different credentials.

For a **new empty database**:

```powershell
.\.venv\Scripts\python.exe -m flask --app app db upgrade
.\.venv\Scripts\python.exe -m flask --app app create-user
.\.venv\Scripts\python.exe app.py
```

Open http://127.0.0.1:5000 and sign in using the website account you created. The password must contain at least 12 characters. Sign-in accounts are provisioned by an operator; public registration and password recovery are not implemented.

For an existing installation whose two tables were created with `init-db`, back up the database and compare its schema with migration `0001` before running `flask --app app db stamp 0001`. Stamping records a migration version; it does not change or validate the schema. Do not run the initial upgrade over pre-existing tables. Future schema changes should use reviewed migrations.

## CSV uploads

Files must be UTF-8 CSV with the exact column names below. The combined request limit is **10 MiB**. Invalid uploads are rejected rather than silently dropping records. Each successful submission creates a new analysis, including repeat uploads.

### Transactions

Upload one CSV, with up to 10,000 rows:

```csv
record_id,step,type,amount,oldbalanceOrg,newbalanceOrig,oldbalanceDest,newbalanceDest
example-normal,10,PAYMENT,100,500,400,0,0
```

`record_id` is optional; if present it must be unique within the upload. `step` is a nonnegative integer. Empty amount/balance cells are retained as missing values. Notice the exact spelling of `oldbalanceOrg` and `newbalanceOrig`.

### Journals

Upload all three CSVs together and select the sector:

| File / upload field | Columns | Maximum rows |
| --- | --- | --- |
| Headers / `headers` | `document_id,posting_date,document_date,document_type,source,business_process` | 1,000 |
| Lines / `lines` | `document_id,line_number,gl_account,debit_amount,credit_amount,local_amount,auxiliary_account_number` | 10,000 |
| Accounts / `chart_of_accounts` | `account_number,account_type` | 10,000 |

Use dates in `YYYY-MM-DD` format. Header document IDs must be unique; each line must reference an existing header. Account codes are strings, so leading zeros are preserved. Account types are `Asset`, `Liability`, `Equity`, `Tiers`, `Expense`, `Revenue`, or `Special`.

### Demonstration data

All included upload examples are **synthetic**. `ml-engine/examples/input/demo-scenarios/` contains 20 transactions and 12 journals designed to demonstrate different review reasons. Upload `transactions_demo.csv` for transactions, or the three ledger CSVs together using **healthcare**.

With the bundled models, the transaction examples produce 5 model alerts, 10 separate rule alerts, and 5 normal records. The healthcare journal examples produce 1 model alert, 8 separate rule alerts, and 3 entries without alerts. Search for `demo-` to distinguish them from previous uploads. Missing amounts, repeated line numbers, and accounting inconsistencies in these examples are intentional. These fixtures demonstrate behavior; they do not measure model accuracy.

## API

Browser requests use an authenticated session and a CSRF token for writes. Integration clients use `Authorization: Bearer <DALEEL_API_TOKEN>`. The token grants access to the shared workspace; keep it server-side.

| Route | Purpose |
| --- | --- |
| `GET /health` | Public process/model readiness |
| `GET /health/db` | Authenticated database connectivity check |
| `GET /v1/models` | Model identity, limits, and supported sectors |
| `POST /v1/transactions/predict` | Transaction JSON predictions without saving |
| `POST /v1/ledger/predict` | Journal JSON predictions without saving |
| `POST /analyses/transactions` and `/analyses/ledger` | JSON predictions with persistence |
| `POST /analyses/transactions/upload` | Multipart CSV field `file` |
| `POST /analyses/ledger/upload` | Multipart journal files plus `sector` |
| `GET /analyses` and `/analyses/<id>` | Saved analyses and detail |
| `GET /workspace?periodDays=30` | Dashboard projection; periods 7, 30, or 90 |

JSON examples are under `examples/`. `api/openapi.json` describes the prediction API, not every dashboard route. Requests are synchronous; a busy model returns HTTP 429. Validation failures return 422, oversized requests 413, and storage failures 503. Do not blindly retry saved-analysis POST requests: they are not idempotent.

## Deployment

Deploy the application as a Python web service or Docker container with a PostgreSQL database. GitHub stores the source; GitHub Pages cannot run Flask or PostgreSQL. The browser should use the same HTTPS origin as the Flask service.

Build command: `python -m pip install -r requirements.txt`

Start command: `python app.py`

Run `python -m flask --app app db upgrade` once as a release task for a new database, then create an operator-managed account with `python -m flask --app app create-user`. Do not run database migrations independently in every web worker.

Set these values in the host's secret/environment settings:

| Variable | Configuration |
| --- | --- |
| `DALEEL_ENV` | `production` |
| `DALEEL_API_TOKEN` | A newly generated random token, at least 32 printable ASCII characters without spaces |
| `DATABASE_URL` | Host-provided PostgreSQL connection URL; URL-encode special characters in credentials |
| `DB_SSLMODE` | Provider-required TLS mode; prefer `verify-full` with the provider's trusted certificate configuration |
| `DALEEL_ALLOWED_HOSTS` | Comma-separated hostnames without schemes, e.g. `daleel.example.com` |
| `DALEEL_COOKIE_SECURE` | `1`; production refuses insecure cookies |
| `PORT` | Host-assigned port; takes precedence over `DALEEL_PORT` |
| `DALEEL_HOST` | `0.0.0.0` for hosting; production defaults to this |
| `DALEEL_TRUSTED_PROXY` | Actual upstream proxy address, only when deployed behind a reverse proxy |
| `DALEEL_PROXY_COUNT` | Number of trusted proxy hops; default `1` |

For hosts with changing proxy addresses, `DALEEL_TRUSTED_PROXY=*` is appropriate **only** when network access to the application is restricted to that trusted proxy. Configure the proxy to supply the original HTTPS scheme, client IP, and public host. Do not trust arbitrary forwarded headers from direct Internet clients. Correct proxy settings are necessary for CSRF origin checks and client-IP sign-in throttling. Waitress removes untrusted proxy headers by default. See the [Waitress proxy documentation](https://docs.pylonsproject.org/projects/waitress/en/latest/arguments.html).

The server must be behind HTTPS in production. Secure cookies, host validation, security headers, and the production-only content security policy are enabled by configuration. The token-based developer page `/upload` is disabled in production. Model artifacts, configuration files, database backups, and source files have no public download routes.

For Docker:

```sh
docker build -t daleel .
docker run --rm --env-file /secure/path/daleel.env -p 127.0.0.1:5000:5000 daleel
```

The image runs as a non-root user and does not copy `.env`, training datasets, logs, or tests. Provide a database reachable from the container: `localhost` inside the container is not your laptop's PostgreSQL server. Run schema/account commands using the same image and environment. Configure HTTPS at the hosting platform or reverse proxy.

Keep one application instance for the current shared-workspace release. Inference locking and sign-in throttling are process-local. Horizontal scaling requires a shared rate limiter and an explicit workload/concurrency plan. Measure memory and response times on the chosen host with representative uploads before public use. Database backups and retention are the operator's responsibility.

## Verification

Run from the project directory in the installed environment:

```sh
python -m pip check
python -m unittest discover -s tests -v
python -m unittest test_backend_storage -v
python -m unittest discover -s ml-engine/tests -p "test_*ml*.py" -v
node --check frontend/app.js
node --check frontend/workspace.js
node --check frontend/integration.js
```

Storage tests use an isolated SQLite database and never delete production analyses. API tests load the bundled models. ML tests also exercise feature handling and small synthetic training fixtures. GitHub Actions runs the suite and a Docker build on Linux. Passing local tests does not substitute for checking the hosted PostgreSQL connection, HTTPS login/logout, both upload flows, and persistence after a restart.

## Scope and limitations

- This release is a **shared team workspace**: every signed-in account can see the same analyses. It does not isolate customer organizations or user-owned data.
- Review decisions, generated reports, profile editing, and password recovery are not implemented; unsupported frontend actions remain disabled.
- Dates in dashboard periods refer to analysis creation time. Detail views show at most 2,000 records per type and 300 source files, with a truncation notice.
- The database retains predictions and selected metadata, not complete original uploaded CSV files. It is not a replacement for an accounting system.
- The models were trained on synthetic/simulated datasets. Strong offline metrics do not establish performance on real business data. Validate on representative, appropriately labeled data before using results for consequential decisions.

## Data provenance and attribution

Training metadata identifies these sources and fixed revisions:

- [Financial transaction dataset](https://huggingface.co/datasets/RegularGuyOIO/financial-transaction-fraud-dataset), revision `0af54eb7e70f642a416d4022b03a2cb360efbd69`.
- [ForensicBench](https://huggingface.co/datasets/WaguyMZ/ForensicBench), revision `9c259ac178a9aeddf4a1ea509cf526bbccb5c04a`.

The raw training datasets are not included. Model metadata remains beside the trained artifacts. The bundled IBM Plex Sans Arabic font retains its upstream SIL Open Font License in `frontend/assets/OFL.txt`.

No project-wide open-source license has been assigned by the contributors. Public visibility alone does not grant a general reuse license. Contributors should agree on a project license and confirm source-data/model redistribution terms before publishing the repository. Third-party licenses remain applicable.
