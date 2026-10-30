# Daleel | دليل

Daleel is a financial review workspace that helps teams identify transactions and journal entries that deserve closer inspection. It combines an Arabic, right-to-left web interface, trained machine-learning models, accounting checks, and a PostgreSQL analysis history in one Flask application.

Upload a transaction CSV or a set of journal CSVs, inspect the results, and return to saved analyses without rerunning the models. The goal is to reduce manual screening and make review evidence easier to find. An alert indicates a need for investigation; it does not establish fraud.
<img width="1522" height="878" alt="image" src="https://github.com/user-attachments/assets/8ad871cd-df0e-433b-a376-8b5520239d7e" />


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
<img width="1453" height="816" alt="image" src="https://github.com/user-attachments/assets/80af13a7-2deb-4e8f-9023-ef4f2ff6a20a" />
## Algorithms and interpretation

| Component | Algorithm | Role |
| --- | --- | --- |
| Transaction inference | Histogram-based Gradient Boosting (`HistGradientBoostingClassifier`) | Selected deployed classifier |
| Transaction experiments | Random Forest; economic, deep, and regularized XGBoost variants; extended histogram boosting | Compared during training; these are not all used for each prediction |
| Journal inference | Isolation Forest | Separate anomaly-ranking models for five sectors |
| Validation rules | Deterministic checks | Independent evidence, not a machine-learning algorithm |
