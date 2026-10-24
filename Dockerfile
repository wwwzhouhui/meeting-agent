FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY examples ./examples
COPY web ./web
COPY config ./config
# Dependency list must mirror pyproject.toml [project.dependencies]
# (pydantic, fastapi, uvicorn, python-dotenv, httpx, APScheduler,
# pyyaml, psycopg, python-multipart). httpx is required: without it the
# LLM client and FastAPI TestClient break in the image. APScheduler
# drives the optional background jobs; pyyaml loads the orchestration
# config (config/orchestration.yaml — copied above so the API in the
# image reads the same switches as a local run); psycopg is the
# Postgres driver for DATABASE_URL mode; python-multipart parses the
# /audio upload. web/ is copied so the zero-build UI is served by the
# API (GET /) in the image too.
# The ASR extra (faster-whisper) is NOT in the image by default — add
# `pip install faster-whisper` + ASR_ENGINE=faster-whisper to use it.
RUN pip install --no-cache-dir "pydantic>=2" fastapi uvicorn python-dotenv httpx "APScheduler>=3.10" "pyyaml>=6" "psycopg[binary]>=3.1" python-multipart
EXPOSE 8000
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
