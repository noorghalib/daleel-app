"""Run with: python -m service.run (from the handoff folder)."""
import os

# Set before importing numpy/sklearn. One process; no reload or training here.
for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[name] = "8"


def main():
    from waitress import serve
    from .app import create_app, LIMITS
    app = create_app()
    serve(app, host=os.environ.get("DALEEL_HOST", "127.0.0.1"),
          port=int(os.environ.get("DALEEL_PORT", "8000")), threads=4,
          max_request_body_size=LIMITS["body_bytes"], expose_tracebacks=False)


if __name__ == "__main__":
    main()
