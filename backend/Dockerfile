# One image, six services.
#
# yard-api, procurement-api, dashboard-gateway, dock-worker, match-worker,
# supplier-agent and simulator all import the same `shared/` package and the
# same requirements.txt. Six near-identical Dockerfiles would be six places to
# forget to update, so this builds one image and compose selects the entry
# point per service with `command:`. That also means the layer cache is shared:
# the dependency install happens once for the whole backend, not seven times.
#
# Build context is the REPO ROOT, not backend/ -- see docker-compose.yml. The
# image still needs only backend/, but keeping the context at the root is what
# lets .dockerignore be written once for everything.

FROM python:3.12-slim

# PYTHONUNBUFFERED: without it, a crashing worker's traceback can sit in a
# stdio buffer and never reach `docker compose logs`, which is the one place
# anyone will look.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies before source: requirements.txt changes rarely, application code
# changes constantly, so this layer survives almost every rebuild. ortools,
# psycopg2-binary, bcrypt and pillow all publish manylinux wheels for amd64 and
# aarch64, so no compiler is needed in the image.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/

# Procurement API writes rendered invoice images here (INVOICE_DIR, default
# BACKEND_ROOT.parent/data/invoices = /app/data/invoices) and mkdirs it at
# import. Creating it in the image first means the named volume mounted over it
# inherits this ownership instead of arriving root-owned and unwritable.
RUN mkdir -p /app/data/invoices \
 && useradd --create-home --uid 10001 app \
 && chown -R app:app /app
USER app

# Every service does sys.path.insert(0, BACKEND_ROOT) itself, so both the
# uvicorn module paths (services.yard_api.main:app) and the bare worker scripts
# (services/dock_worker/main.py) resolve `shared` from here.
WORKDIR /app/backend

EXPOSE 8001 8002 8003 8004

# No default service: this image is only ever run with an explicit command.
# Failing loudly beats silently starting the wrong one.
CMD ["python", "-c", "import sys; sys.exit('This image needs an explicit command -- see docker-compose.yml (yard-api, procurement-api, dashboard-gateway, dock-worker, match-worker, supplier-agent, simulator).')"]
