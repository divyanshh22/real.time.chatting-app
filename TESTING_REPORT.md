# Testing Report — Django Real-Time Chat Application

**Date:** 2026-11-04
**Repository:** `divyanshh22/real.time.chatting-app` (branch `main`)
**Test environment (local, measured):** Windows, Python 3.13.7, Django 6.0.5, SQLite in-memory, in-memory Channels layer
**CI environment (configured, not yet executed):** GitHub Actions, Ubuntu, Python 3.13, PostgreSQL 16 + Redis 7

---

## 1. Summary

| Metric | Result |
|---|---|
| Tests collected / run | **209** (204 new pytest tests + 5 pre-existing legacy tests) |
| Passed | **209** |
| Failed | **0** |
| Skipped / xfailed | **0 / 0** (no skips — every test executes) |
| Duration | ~32 s |
| Line coverage of application code | **88.96 %** (589 statements, 65 missed → reported as 89 %) |
| CI coverage gate | `--cov-fail-under=88` (measured value floored) |
| `ruff check .` | **All checks passed** (0 errors) |
| `ruff format --check` (new files) | **13 files already formatted** |
| `python manage.py check` | `System check identified no issues (0 silenced)` (exit 0) |
| `python manage.py makemigrations --check --dry-run` | `No changes detected` (exit 0) |
| GitHub Actions CI | **Not yet run on GitHub** — the workflow is committed and locally validated, but it has not executed against the remote until the code is pushed (see §9) |

## 2. How to run

```bash
pip install -r requirements-dev.txt   # pytest + plugins + ruff
pytest                                # full suite (coverage on by default via pytest.ini)
pytest -m websocket                   # only the WebSocket/consumer tests
pytest --cov                          # coverage report
pytest --cov-fail-under=88            # exact command CI runs
ruff check .                          # lint (config in ruff.toml)
ruff format --check tests/ login/utils.py settings_test.py   # format check (new files only)
```

Real terminal output of the final validation run (`pytest --cov-fail-under=88`):

```text
................................. [100%] (dots for all 209 tests, none failed)
=============================== tests coverage ================================
Name                        Stmts   Miss  Cover
-------------------------------------------------
chat/consumers.py             105     12    89%
chat/presence_consumer.py      54      0   100%
chat/views.py                 168     35    79%
login/views.py                102      5    95%
login/utils.py                 21      3    86%
... (full table in §5)
TOTAL                         589     65    89%
Coverage XML written to file coverage.xml
Required test coverage of 88% reached. Total coverage: 88.96%
209 passed, 113 warnings in 32.16s
```

> All 113 warnings are the *same* non-fatal `UserWarning: No directory at: …\staticfiles\`
> emitted once per test-client use (the directory only exists after `collectstatic`).
> It is cosmetic and does not affect any assertion.

## 3. Tests per file

| File | Tests | What it covers |
|---|---:|---|
| `tests/test_views.py` | 50 | URL resolution, auth-protected endpoints, home inbox, conversation page, message/history APIs, search, read-receipt flow, wrong-method handling, cross-user isolation |
| `tests/test_consumers.py` | 36 | `ChatConsumer` + `PresenceConsumer` over the **real ASGI app**: connect/accept, message send & broadcast, typing, read receipts, presence, group isolation, malformed frames, disconnect edge cases |
| `tests/test_security.py` | 33 | Credential leakage, anonymous access, CSRF enforcement, SQL-injection-like input, XSS escaping, `javascript:` URI inertness, Cloudinary/network isolation, production-settings sanity (subprocess with `DEBUG=false/true`) |
| `tests/test_models.py` | 20 | `CustomUser`, `ProfilePic`, `Message` field behavior, defaults, string representations, relations |
| `tests/test_forms.py` | 23 | View-level validation (the project has **no forms.py**): register/login required fields, duplicate username/email, invalid email, weak password, empty messages, image type **and size** validation |
| `tests/test_health.py` | 19 | `manage.py check`, `makemigrations --check`, migration plan applied, clean migrate from scratch, admin pages, template compilation, every page renders |
| `tests/test_auth.py` | 15 | Login/logout/register flows, session key, password hashing (MD5 hasher in tests), redirect behavior |
| `tests/test_routing.py` | 8 | WebSocket URL patterns, ASGI protocol routing, route kwargs |
| `chat/tests.py` *(legacy)* | 2 | Pre-existing `TestCase` tests — kept untouched, still collected and passing |
| `login/tests.py` *(legacy)* | 3 | Pre-existing `TestCase` tests — kept untouched, still collected and passing |
| **Total** | **209** | |

Markers (enforced with `--strict-markers`): `unit`, `integration`, `websocket`, `security`.

## 4. Isolation — no external service is ever touched

- **Database:** SQLite `:memory:` locally; `DATABASE_URL` → PostgreSQL in CI (`settings_test.py` decides).
- **Redis / Channels:** always `InMemoryChannelLayer`, `USE_REDIS = False`.
- **Cloudinary:** forced off; filesystem storage in a per-run temp dir. Tests spy on `cloudinary.uploader.upload` and assert it is **never called**.
- **Network:** an autouse fixture blocks non-loopback DNS/connections (loopback stays open for asyncio internals and CI services). Tests assert the blocker actually raises for `example.com` / `api.cloudinary.com`.
- **Email:** `locmem` backend. **Hashing:** MD5 hasher (fast). **`DEBUG`:** `False`, `SECURE_SSL_REDIRECT` disabled for plain-HTTP tests.
- Production settings (secure-cookie flags, real `SECRET_KEY` handling) are verified in a **subprocess** with `DEBUG=false`, so the test overrides cannot mask them.

## 5. Coverage of application code (measured, `--cov` with `.coveragerc`)

`.coveragerc` omits `.venv`, tests, migrations, `manage.py`, `settings_test.py`, and dev scripts — i.e. only real application code is measured.

| Module | Stmts | Miss | Cover |
|---|---:|---:|---:|
| `chat/admin.py` | 7 | 0 | 100 % |
| `chat/apps.py` | 3 | 0 | 100 % |
| `chat/consumers.py` | 105 | 12 | 89 % |
| `chat/models.py` | 13 | 0 | 100 % |
| `chat/presence_consumer.py` | 54 | 0 | 100 % |
| `chat/routing.py` | 4 | 0 | 100 % |
| `chat/urls.py` | 4 | 0 | 100 % |
| `chat/views.py` | 168 | 35 | 79 % |
| `chattingapp/asgi.py` | 8 | 0 | 100 % |
| `chattingapp/settings.py` | 43 | 8 | 81 % |
| `chattingapp/urls.py` | 10 | 1 | 90 % |
| `login/admin.py` | 15 | 0 | 100 % |
| `login/apps.py` | 5 | 0 | 100 % |
| `login/models.py` | 13 | 0 | 100 % |
| `login/signals.py` | 7 | 0 | 100 % |
| `login/urls.py` | 7 | 1 | 86 % |
| `login/utils.py` | 21 | 3 | 86 % |
| `login/views.py` | 102 | 5 | 95 % |
| **TOTAL** | **589** | **65** | **88.96 %** |

Where the missed lines are (honest breakdown):

- `chat/views.py` — Redis-presence fallbacks in `get_user_presence` (Redis is disabled in tests), the `except Exception: pass` broadcast-failure branches, and one unreachable sorting branch (see §6, bug 10).
- `settings.py` — the `if not DEBUG:` secure-flag block: exercised by the subprocess tests, whose coverage is **not** counted in this in-process report (the behavior itself is asserted).
- `login/views.py` / `login/utils.py` — narrow defensive branches (creation-failure handler, file-seek error path).
- The CI gate is the floored measured value: **88.96 % → `--cov-fail-under=88`**.

## 6. Application bugs found by the tests and fixed

All fixes are in commit `c3951dd` (kept separate from the tests and the CI, as planned).
Every fix is minimal and covered by a regression test.

| # | Bug | Symptom | Fix + test |
|---|---|---|---|
| 1 | `ChatConsumer.receive` parsed frames blindly | Invalid JSON → `ValueError`; non-dict JSON → `AttributeError`; non-string `text` → crash. Any browser/proxy could 500 the consumer | Validate JSON/dict/`str` before use; malformed frames are dropped. `tests/test_consumers.py` |
| 2 | `PresenceConsumer.receive(text_data)` | Binary frames raised `TypeError` (wrong signature); invalid JSON/non-dict crashed | Signature is now `receive(text_data=None, bytes_data=None)` + validation. `tests/test_consumers.py` |
| 3 | `ChatConsumer.disconnect` without a completed `connect` | `AttributeError: no room_group_name` (disconnect-before-connect, anonymous user, unknown chat partner) | Guard clause; nothing to clean up → return. `test_disconnect_without_connect_does_not_crash` |
| 4 | `login_view` / `register_view` used `request.POST['…']` | Missing form keys → `KeyError` → **HTTP 500** on a trivially crafted POST | `.get()` + required-field validation with a friendly message. `tests/test_forms.py` |
| 5 | Registration accepted weak passwords | `create_user()` never runs validators — `"123"` was a valid password | `django.contrib.auth.password_validation.validate_password`. `test_register_weak_password_rejected` |
| 6 | Registration accepted malformed emails | No format validation | `validate_email`. `test_register_invalid_email_rejected` |
| 7 | **Any** file accepted as profile picture / message photo | A `.txt` renamed `.png`, an exe, or a 200 MB file was stored (and served to others) | New shared helper `login/utils.py::is_valid_image_upload`: content-type check + 5 MB cap + Pillow `verify()` (catches forged content types). Wired into profile view (redirect), message API (**400**), conversation POST (redirect). 6 tests in `tests/test_forms.py` |
| 8 | NUL bytes (`\x00`) in message text | Stored fine on SQLite but **crashed PostgreSQL inserts** (`psycopg2`/`psycopg` `ValueError`) → 500 on the production database | Stripped in both HTTP save paths and the WebSocket consumer. `test_null_byte_in_message_rejected_not_crashing` |
| 9 | Malformed `message_ids` / `user_ids` lists | `["abc", null, {}]` sent over WebSocket → ORM `Field 'id' expected a number` crash | Integer-only filtering (booleans excluded), non-list payloads ignored. `tests/test_consumers.py` |
| 10 | *(latent, documented — **not** fixed)* `chat/views.py:62` uses `timezone.utc` | `django.utils.timezone.utc` no longer exists in Django ≥ 5 — the expression would raise `AttributeError`. It is currently unreachable (the `else` branch only runs for chat users without messages, which cannot occur), so per the "report, don't fix" rule it is left as-is and explains one of the missed coverage lines | — |

Also fixed as pure lint cleanups (commit `6d41ec3`, no behavior change): unused `import django` in `chattingapp/asgi.py`, unused `user` variable in `login/views.py`, and an explicit `# noqa: F401` on the side-effect import in `login/apps.py`.

A note on my own work: while linting, ruff caught that my first version of fix #7 in `conversation_view` called `messages.error(…)` which — because that view has a local `messages` **QuerySet** — would have raised `AttributeError`. The import is now aliased (`django_messages`), and `test_conversation_post_rejects_non_image` is a permanent regression test for exactly that path (verified: it fails with `AttributeError` if the alias is removed).

## 7. Skipped / expected-failure tests

**None.** The suite reports `0 skipped`, `0 xfail`:

- No `@pytest.mark.skip` / `skipif` exists anywhere.
- External services are mocked or isolated rather than skipped.
- The one conditional branch (`test_null_byte…` accepts 200 *or* 400) asserts one of the two legal outcomes instead of skipping.

## 8. CI/CD pipeline (`.github/workflows/ci.yml`)

Triggers: `push` and `pull_request` to `main`. `permissions: contents: read`, concurrency cancels outdated runs.

| Job | Steps |
|---|---|
| **lint** | Python 3.13 (matches `Dockerfile`'s `python:3.13-slim`) → `pip install -r requirements-dev.txt` → `ruff check .` → `ruff format --check tests/ login/utils.py settings_test.py` |
| **test** | Services: `postgres:16-alpine` + `redis:7-alpine` (both with health checks) → install `requirements.txt` + `requirements-dev.txt` (pip cache) → `python manage.py check` → `python manage.py makemigrations --check --dry-run` → `DJANGO_SETTINGS_MODULE=settings_test pytest --cov-fail-under=88` → upload `coverage.xml` artifact (also on failure) |
| **docker-build** | `docker build -t chattingapp:ci .` — verifies the Dockerfile, **never pushes** |

Locally validated: the YAML parses (pyyaml), the lint steps pass verbatim, both Django check commands exit 0, and `pytest --cov-fail-under=88` exits 0. A `.dockerignore` was extended (keeping all pre-existing entries) so the build context excludes `.venv`, `.git`, `.env`, caches, and docs.

**Scope decision (documented in `ruff.toml`):** `ruff format` is checked only on the files this work added. Reformatting the whole repository would rewrite **thousands of lines** of existing application code (~31 files), which is a style overhaul, not a "trivial fix" — so it was deliberately left out of scope. Lint rules are likewise minimal (pyflakes + critical pycodestyle), chosen because they catch real bugs without churning existing code.

## 9. Honest limitations

1. **CI has not run on GitHub yet.** The workflow file is committed and every step was validated locally, but no green checkmark exists until the code is pushed and Actions executes it.
2. **Local runs used SQLite; CI uses PostgreSQL.** No credentials for the local PostgreSQL server were available (connection requires a password) and Docker is not installed in this environment, so the 209-test/88.96 % measurement is from SQLite in-memory. The suite contains no raw SQL (verified), only portable ORM calls, and the PostgreSQL-specific hazards (NUL bytes, `LIKE` wildcards) are covered by dedicated tests — but the PostgreSQL execution itself is pending CI.
3. **No browser-level E2E tests.** WebSockets are tested through Django Channels communicators over the real ASGI app (session cookie authentication included), not through a real browser. No Selenium/Playwright was introduced.
4. **Line coverage only** (no branch coverage), measured in-process — subprocess-based tests (`manage.py check`, production-settings checks) contribute assertions but not coverage lines.
5. **Known product limitations, documented by tests rather than "fixed"** (they are design decisions, out of scope to change):
   - successful login POST always redirects to the chat home — a `?next=` target is not honored;
   - message history is not paginated (the full thread is loaded);
   - with no `SECRET_KEY` environment variable the production settings fall back to a hardcoded insecure key (asserted by `test_production_settings_default_secret_key_is_used_when_env_missing` — deployment **must** set the env var);
   - `DEBUG` defaults to `True` when the env var is unset, which disables all secure-cookie/SSL flags (asserted by `test_debug_true_disables_secure_flags`).
6. The 5 pre-existing `TestCase`-style tests in `chat/tests.py` / `login/tests.py` were intentionally left untouched; they are still collected by pytest and pass.
7. The README CI badge will show "no status" until the first workflow run completes on GitHub.

## 10. Next steps

1. **Push to `origin/main`** (6 new commits: `1e380ee` infra → `96b87c1` tests → `c3951dd` app fixes → `6d41ec3` CI → `5b8bc74` README → report), then open the **Actions** tab: the expected result is three green jobs (lint / test / docker-build) and a populated README badge.
2. Watch the first **test** job especially: it is the first execution against PostgreSQL. If a divergence appears, the failing test name will pinpoint it (all analysis so far says it should pass).
3. Optional deployment hardening: configure the host (e.g. Render) to **wait for CI** before deploying, so a red build never reaches production.
4. Coverage follow-ups if desired: exercise the Redis-presence fallback in `chat/views.py` (needs a Redis-enabled settings variant) to push `chat/views.py` past 90 %.
5. Optional cleanup: suppress the cosmetic `staticfiles` `UserWarning` in `pytest.ini` (`filterwarnings`) to take the suite from 113 warnings to 0.
