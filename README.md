# Django-Real-Time-Chat-Application

[![CI](https://github.com/divyanshh22/real.time.chatting-app/actions/workflows/ci.yml/badge.svg)](https://github.com/divyanshh22/real.time.chatting-app/actions/workflows/ci.yml)

A real-time chat application built with Django, Channels, and WebSockets.

## Tech Stack

| Layer            | Technology                                      |
|------------------|--------------------------------------------------|
| Backend          | Python 3.13, Django 5/6                          |
| Real-Time        | Django Channels 4, WebSockets                    |
| ASGI Server      | Daphne                                           |
| Reverse Proxy    | Nginx (static/media serving, WebSocket proxying) |
| Database         | PostgreSQL                                       |
| Channel Layer    | Redis (pub/sub)                                  |
| Static Files     | WhiteNoise + Nginx                               |
| Media Storage    | Cloudinary / local media                         |
| Containerization | Docker, Docker Compose                           |

## Features

- Real-time one-to-one chat over WebSockets
- Redis-backed channel layer for multi-worker scaling
- Custom user model with authentication
- Media/image uploads
- Production-ready settings via environment variables

## Quick Start with Docker

Requires Docker and Docker Compose.

```bash
# Build and start all services (Nginx + web + PostgreSQL + Redis)
docker compose up --build
```

Then open http://localhost

Architecture: **Nginx** listens on port 80, serves `/static/` and `/media/` directly, and proxies HTTP + WebSocket (`/ws/`) traffic to **Daphne** inside the `web` container.

The entrypoint automatically runs migrations and collects static files before starting Daphne.

### Useful commands

```bash
# Run in background
docker compose up -d

# Create a superuser
docker compose exec web python manage.py createsuperuser

# View logs
docker compose logs -f web

# Stop everything (add -v to wipe database/redis data)
docker compose down
```

## Deploy on Render

The repository ships a [Render Blueprint](https://render.com/docs/blueprints) in `render.yaml`:

1. Render Dashboard → **New → Blueprint** → connect this repository (branch `main`).
2. Render finds `render.yaml` at the repo root and proposes the stack: a web service (build via `./build.sh`, serve with Daphne on Render's `$PORT`) plus a PostgreSQL instance wired into `DATABASE_URL` automatically.
3. Click **Deploy Blueprint**. Optional: fill the three `CLOUDINARY_*` values in the service's **Environment** tab so uploaded media survives redeploys.

Notes: the Free Postgres plan expires 30 days after creation (upgrade it to keep the data), and a single web instance uses the in-memory Channels layer — add a Key Value resource and set `USE_REDIS=true` + `REDIS_URL` before scaling beyond one instance.

## Local Development (without Docker)

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt

python manage.py migrate
daphne -b 0.0.0.0 -p 8000 chattingapp.asgi:application
```

By default the channel layer is in-memory; set `USE_REDIS=true` and `REDIS_URL` to use Redis.

## Environment Variables

| Variable        | Description                              | Default                  |
|-----------------|------------------------------------------|--------------------------|
| `SECRET_KEY`    | Django secret key                        | insecure dev key         |
| `DEBUG`         | Enable debug mode (`True`/`False`)       | `True`                   |
| `ALLOWED_HOSTS` | Comma-separated hosts                    | `*`                      |
| `DATABASE_URL`  | Postgres connection string               | local postgres           |
| `USE_REDIS`     | Enable Redis channel layer               | `false`                  |
| `REDIS_URL`     | Redis connection string                  | `redis://localhost:6379` |

## Testing

The project ships with a pytest-only test suite (no `unittest.TestCase` in the
new tests) covering models, view-level validation, auth, consumers/WebSockets,
URL routing, security, and project health (system checks + migrations).

```bash
# 1. Install the dev/test dependencies (production requirements stay separate)
pip install -r requirements-dev.txt

# 2. Run the full suite (coverage is enabled by default via pytest.ini addopts)
pytest

# 3. Run only the WebSocket/consumer tests
pytest -m websocket

# 4. Run with a coverage report
pytest --cov
```

Useful variants:

```bash
pytest -m security          # security-focused tests only
pytest tests/test_views.py  # a single file
pytest -x                   # stop at the first failure
```

Notes:

- Tests run with `settings_test.py` (selected automatically by `pytest.ini`):
  in-memory SQLite locally (PostgreSQL in CI via `DATABASE_URL`), an
  in-memory Channels layer, the filesystem storage instead of Cloudinary, and
  a network blocker that forbids non-local connections. No Redis, Cloudinary,
  or other external services are ever contacted.
- Current measured line coverage of the application code: **88.96%**
  (589 statements); CI enforces `--cov-fail-under=88`.
- CI (`.github/workflows/ci.yml`) runs on every push/PR to `main`:
  `ruff check` + `ruff format --check` (new files), `manage.py check`,
  `makemigrations --check --dry-run`, the full pytest suite against
  PostgreSQL, and a Docker image build (never pushed).
