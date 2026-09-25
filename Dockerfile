# syntax=docker/dockerfile:1
#
# Two images from one file:
#   docker build --target api -t chartsight-api .   # FastAPI service (default target)
#   docker build --target ui  -t chartsight-ui  .   # Streamlit UI, a client of the API
# Or run both: docker compose up --build

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CHARTSIGHT_DATA_DIR=/app/data
WORKDIR /app
RUN useradd --create-home --uid 10001 chartsight


# Install dependencies from pyproject against a stub package first, so this layer is
# cached until the dependency list changes, not every time the code does.
FROM base AS deps-api
COPY pyproject.toml README.md ./
RUN mkdir chartsight && touch chartsight/__init__.py \
    && pip install ".[api]" && pip uninstall -y chartsight && rm -rf chartsight

FROM base AS deps-ui
COPY pyproject.toml README.md ./
RUN mkdir chartsight && touch chartsight/__init__.py \
    && pip install ".[ui]" && pip uninstall -y chartsight && rm -rf chartsight


FROM deps-ui AS ui
COPY chartsight ./chartsight
RUN pip install --no-deps .
COPY data ./data
COPY app.py ./
USER chartsight
EXPOSE 8501
# Set CHARTSIGHT_API_URL to use the API; without it the UI runs the pipeline in-process.
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]


# Last stage = the default `docker build` target.
FROM deps-api AS api
COPY chartsight ./chartsight
RUN pip install --no-deps .
COPY data ./data
USER chartsight
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
CMD ["uvicorn", "chartsight.api:app", "--host", "0.0.0.0", "--port", "8000"]
