# Django-Real-Time-Chat-Application

A real-time chat application built with Django, Channels, and WebSockets.

## Tech Stack

| Layer            | Technology                                      |
|------------------|--------------------------------------------------|
| Backend          | Python 3.13, Django 5/6                          |
| Real-Time        | Django Channels 4, WebSockets                    |
| ASGI Server      | Daphne                                           |
| Database         | PostgreSQL                                       |
| Channel Layer    | Redis (pub/sub)                                  |
| Static Files     | WhiteNoise                                       |
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
# Build and start all services (web + PostgreSQL + Redis)
docker compose up --build
```

Then open http://localhost:8000

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
