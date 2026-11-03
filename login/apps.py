from django.apps import AppConfig


class LoginConfig(AppConfig):
    name = 'login'

    def ready(self):
        import login.signals  # noqa: F401 — imported for its signal handlers