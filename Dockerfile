# syntax=docker/dockerfile:1
#
# One image: the FastAPI service, which also serves the web UI at /.
#   docker build -t chartsight .
#   docker run -p 8000:8000 chartsight     # UI http://localhost:8000, API docs /docs
# Or: docker compose up --build

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PIP_NO_CACHE_DIR=1     PIP_DISABLE_PIP_VERSION_CHECK=1     CHARTSIGHT_DATA_DIR=/app/data
WORKDIR /app
RUN useradd --create-home --uid 10001 chartsight


# Install dependencies from pyproject against a stub package first, so this layer is
# cached until the dependency list changes, not every time the code does.
FROM base AS deps
COPY pyproject.toml README.md ./
RUN mkdir chartsight && touch chartsight/__init__.py     && pip install ".[api]" && pip uninstall -y chartsight && rm -rf chartsight


FROM deps AS api
# AWS Lambda Web Adapter: lets this same image run on Lambda (it forwards Lambda invocations
# to uvicorn as plain HTTP). It is a Lambda extension, so it does nothing anywhere else.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0 /lambda-adapter /opt/extensions/lambda-adapter
ENV AWS_LWA_PORT=8000 \
    AWS_LWA_READINESS_CHECK_PATH=/health
COPY chartsight ./chartsight
RUN pip install --no-deps .
COPY data ./data
USER chartsight
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3     CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
CMD ["uvicorn", "chartsight.api:app", "--host", "0.0.0.0", "--port", "8000"]
