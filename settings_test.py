"""
Test settings for the chattingapp project.

Inherits everything from the real settings and overrides only what tests need:
no Redis, no Cloudinary, no real network services, fast password hashing,
an in-memory channel layer, and a local database.

Database selection:
  * If DATABASE_URL is set in the environment (e.g. PostgreSQL in CI), the
    value parsed by the base settings is kept.
  * Otherwise a SQLite in-memory database is used for local runs.
"""

import os
import tempfile

from chattingapp.settings import *  # noqa: F401,F403  (inherit real settings)

DEBUG = False
SECRET_KEY = "test-secret-key-do-not-use-in-production"
ALLOWED_HOSTS = ["testserver", "localhost"]

# Never talk to Redis in tests: always use the in-memory channel layer.
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    },
}
USE_REDIS = False

# Fast password hashing for tests.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Never send real email.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Never hit real Cloudinary: store media on the local filesystem in a
# per-run temp directory instead (settings.py only enables Cloudinary
# when CLOUDINARY_CLOUD_NAME is set; we force it off regardless).
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "LOCATION": os.path.join(tempfile.gettempdir(), "chattingapp_test_media"),
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}
CLOUDINARY_STORAGE = {}  # no Cloudinary credentials during tests

# Database: keep DATABASE_URL (PostgreSQL in CI) if the environment provides
# one; otherwise use SQLite in-memory for fast local runs.
if not os.environ.get("DATABASE_URL"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

# Tests are executed over plain HTTP; the production SSL redirect (which the
# base settings enable when DEBUG=false in the environment) would turn every
# test-client request into a 301 to https://.
SECURE_SSL_REDIRECT = False
