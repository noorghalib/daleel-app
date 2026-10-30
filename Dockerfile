FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DALEEL_HOST=0.0.0.0 DALEEL_ENV=production
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements*.txt ./
COPY ml-engine/pyproject.toml ml-engine/requirements-lock.txt ./ml-engine/
COPY ml-engine/daleel_ml ./ml-engine/daleel_ml
RUN python -m pip install --no-cache-dir -r requirements.txt && python -m pip check
COPY app.py csv_upload.py workspace_backend.py deployment.py MODEL_IDENTITY.json ./
COPY service ./service
COPY frontend ./frontend
COPY migrations ./migrations
RUN useradd --create-home --uid 10001 daleel
USER daleel
EXPOSE 5000
CMD ["python", "app.py"]
