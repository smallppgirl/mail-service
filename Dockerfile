FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml alembic.ini ./
COPY mail_service ./mail_service
COPY migrations ./migrations
COPY scripts ./scripts
RUN pip install --no-deps . && useradd --uid 10001 --create-home app
USER 10001
EXPOSE 8000
CMD ["uvicorn", "mail_service.api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS tests
USER root
COPY requirements-test.lock ./
RUN pip install --no-cache-dir -r requirements-test.lock
COPY tests ./tests
USER 10001
CMD ["pytest", "-q", "-p", "no:cacheprovider"]
