"""Environment configuration shared by Flask and the production server."""
import os

from sqlalchemy import URL, make_url


def production():
    return os.environ.get("DALEEL_ENV", "development") == "production"


def database_url():
    raw = os.environ.get("DATABASE_URL")
    if raw:
        url = make_url(raw)
        if url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
            raise RuntimeError("DATABASE_URL must use PostgreSQL")
        url = url.set(drivername="postgresql+psycopg")
    else:
        password = os.environ.get("DB_PASSWORD")
        if not password:
            raise RuntimeError("Set DATABASE_URL or DB_PASSWORD")
        url = URL.create("postgresql+psycopg", username=os.environ.get("DB_USER", "daleel_app"),
                         password=password, host=os.environ.get("DB_HOST", "localhost"),
                         port=int(os.environ.get("DB_PORT", "5432")),
                         database=os.environ.get("DB_NAME", "daleel"))
    sslmode = os.environ.get("DB_SSLMODE")
    if sslmode:
        if sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
            raise RuntimeError("Unsupported DB_SSLMODE")
        url = url.update_query_dict({"sslmode": sslmode})
    return url


def configure_app(app):
    is_production = production()
    secure = os.environ.get("DALEEL_COOKIE_SECURE", "1" if production() else "0") == "1"
    hosts = [host.strip() for host in os.environ.get("DALEEL_ALLOWED_HOSTS", "").split(",") if host.strip()]
    if production() and (not secure or not hosts):
        raise RuntimeError("Production requires secure cookies and DALEEL_ALLOWED_HOSTS")
    if is_production and os.environ.get("DALEEL_API_TOKEN", "").upper().startswith("REPLACE_"):
        raise RuntimeError("Replace the example API token before deployment")
    app.config["SESSION_COOKIE_SECURE"] = secure
    if hosts:
        app.config["TRUSTED_HOSTS"] = hosts

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
                "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
        return response


def server_options():
    port = int(os.environ.get("PORT") or os.environ.get("DALEEL_PORT", "5000"))
    if not 1 <= port <= 65535:
        raise RuntimeError("PORT must be between 1 and 65535")
    options = {"host": os.environ.get("DALEEL_HOST", "0.0.0.0" if production() else "127.0.0.1"),
               "port": port, "threads": 4, "expose_tracebacks": False,
               "clear_untrusted_proxy_headers": True}
    proxy = os.environ.get("DALEEL_TRUSTED_PROXY")
    if proxy:
        count = int(os.environ.get("DALEEL_PROXY_COUNT", "1"))
        if count < 1:
            raise RuntimeError("DALEEL_PROXY_COUNT must be positive")
        options.update(trusted_proxy=proxy, trusted_proxy_count=count,
                       trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"})
    return options
