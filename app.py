"""One Flask application for Daleel predictions and PostgreSQL."""
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import jsonify, request, send_file, send_from_directory
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

db = SQLAlchemy()
migrate = Migrate()


class LocalUser(db.Model):
    """Local workspace sign-in; organization permissions are not implemented yet."""
    __tablename__ = "daleel_local_users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.Text, nullable=False)


class Analysis(db.Model):
    """One completed analysis; the original ML response stays intact."""
    __tablename__ = "daleel_analyses"

    id = db.Column(db.String(36), primary_key=True)
    kind = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now())
    model_id = db.Column(db.Text, nullable=False)
    record_count = db.Column(db.Integer, nullable=False)
    result = db.Column(db.JSON, nullable=False)


def create_app():
    load_dotenv(Path(__file__).parent / ".env")
    # Apply before importing the model libraries.
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(name, "8")

    from service.app import create_app as create_ml_app

    from deployment import database_url, configure_app, production

    app = create_ml_app()
    app.config["DALEEL_UPLOAD_UI"] = True
    app.config["DALEEL_PUBLIC_ENDPOINTS"] = {"frontend_page", "frontend_file", "frontend_asset", "upload_page"}
    frontend = Path(__file__).parent / "frontend"

    @app.get("/")
    def frontend_page():
        return send_from_directory(frontend, "index.html")

    @app.get("/styles.css", defaults={"filename": "styles.css"})
    @app.get("/app.js", defaults={"filename": "app.js"})
    @app.get("/workspace.js", defaults={"filename": "workspace.js"})
    @app.get("/integration.js", defaults={"filename": "integration.js"})
    def frontend_file(filename):
        return send_from_directory(frontend, filename)

    @app.get("/assets/<path:filename>")
    def frontend_asset(filename):
        return send_from_directory(frontend / "assets", filename)

    @app.get("/upload")
    def upload_page():
        if production():
            from flask import abort
            abort(404)
        return send_file(Path(__file__).parent / "upload.html")
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url()
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "connect_args": {"connect_timeout": 5},
    }
    db.init_app(app)
    migrate.init_app(app, db)
    from workspace_backend import register_workspace
    register_workspace(app, db, Analysis, LocalUser)
    configure_app(app)

    @app.cli.command("init-db")
    def init_db():
        """Create the initial analysis table without changing existing tables."""
        import click
        db.create_all()
        click.echo("Analysis table is ready.")

    def unavailable():
        db.session.rollback()
        return jsonify(error={"code": "storage_unavailable",
                              "message": "Database operation failed; check database setup."}), 503

    def save_analysis(endpoint, kind, payload=None):
        from uuid import uuid4

        # Reuse the original validation, model loading, concurrency and scoring.
        response = app.make_response(
            app.view_functions[endpoint]() if payload is None
            else app.extensions[f"daleel_predict_{kind}"](payload)
        )
        if response.status_code != 200:
            return response
        result = response.get_json()
        stored_result = dict(result)
        if request.files:
            from werkzeug.utils import secure_filename
            source_files = [{"name": secure_filename(f.filename) or "upload.csv",
                             "size": f.stream.tell()} for f in request.files.values()]
            display_rows = {}
            if payload is not None and kind == "transactions":
                display_rows = {r["record_id"]: {"amount": r["amount"], "type": r["type"]}
                                for r in payload["transactions"]}
            elif payload is not None and kind == "ledger":
                display_rows = {r["document_id"]: {"date": r["posting_date"], "type": r["document_type"]}
                                for r in payload["headers"]}
            stored_result["_source"] = {"files": source_files, "records": display_rows}
        analysis_id = str(uuid4())
        analysis = Analysis(id=analysis_id, kind=kind, model_id=result["model_id"],
                            record_count=result["count"], result=stored_result)
        try:
            db.session.add(analysis)
            db.session.commit()
        except SQLAlchemyError:
            return unavailable()
        return jsonify(analysis_id=analysis_id, **result), 201

    @app.post("/analyses/transactions")
    def analyze_transactions():
        return save_analysis("transactions", "transactions")

    @app.post("/analyses/transactions/upload")
    def upload_transactions():
        from csv_upload import read_transactions
        from service.app import InputError
        files = request.files.getlist("file")
        if len(files) != 1 or set(request.files) != {"file"}:
            raise InputError("Choose exactly one CSV file using the file field")
        records = read_transactions(files[0])
        return save_analysis("transactions", "transactions", {"transactions": records})

    @app.post("/analyses/ledger")
    def analyze_ledger():
        return save_analysis("ledger", "ledger")

    @app.post("/analyses/ledger/upload")
    def upload_ledger():
        from csv_upload import read_ledger_table
        from service.app import InputError, SECTORS
        required = {"headers", "lines", "chart_of_accounts"}
        if set(request.files) != required or any(len(request.files.getlist(name)) != 1 for name in required):
            raise InputError("Choose one headers CSV, one lines CSV, and one accounts CSV")
        if set(request.form) != {"sector"} or len(request.form.getlist("sector")) != 1:
            raise InputError("Choose exactly one sector")
        sector = request.form["sector"]
        if sector not in SECTORS:
            raise InputError("sector must be one of: " + ", ".join(SECTORS))
        payload = {name: read_ledger_table(request.files[name], name) for name in sorted(required)}
        payload.update(sector=sector, review_fraction=0.01)
        return save_analysis("ledger", "ledger", payload)

    @app.get("/analyses")
    def list_analyses():
        from service.app import InputError
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            raise InputError("page must be a positive integer") from None
        if not 1 <= page <= 1000000:
            raise InputError("page must be between 1 and 1000000")
        try:
            records = db.session.execute(db.select(Analysis).order_by(
                Analysis.created_at.desc(), Analysis.id.desc()
            ).offset((page - 1) * 20).limit(21)).scalars().all()
            return jsonify(page=page, has_more=len(records) > 20, analyses=[{
                "analysis_id": row.id, "kind": row.kind,
                "created_at": row.created_at.isoformat(), "model_id": row.model_id,
                "count": row.record_count,
            } for row in records[:20]])
        except SQLAlchemyError:
            return unavailable()

    @app.get("/analyses/<uuid:analysis_id>")
    def get_analysis(analysis_id):
        try:
            record = db.session.get(Analysis, str(analysis_id))
            if record is None:
                return jsonify(error={"code": "not_found", "message": "Analysis not found"}), 404
            return jsonify(analysis_id=record.id, kind=record.kind,
                           created_at=record.created_at.isoformat(), **record.result)
        except SQLAlchemyError:
            return unavailable()

    @app.get("/health/db")
    def database_health():
        # Protected by the existing service authentication hook.
        try:
            db.session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            db.session.rollback()
            return jsonify(status="unavailable", database="disconnected"), 503
        return jsonify(status="ready", database="connected")

    return app


if __name__ == "__main__":
    from waitress import serve
    from deployment import server_options
    application = create_app()
    from service.app import LIMITS
    serve(application, max_request_body_size=LIMITS["body_bytes"], **server_options())
