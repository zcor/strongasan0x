"""DB routers.

BotQueryRouter — engaged when BOT_QUERY_MODE=1 in the environment. Forces
all rollcall_* model reads/writes to the `readonly` alias. The Postgres
role on `readonly` lacks INSERT/UPDATE/DELETE grants, so writes fail at the
DB layer (defence in depth on top of the bot_query verb dispatcher's
namespace boundary). When the env var is unset, the router is a no-op and
normal Django code (web requests, mgmt commands, the bot's own writes via
`default`) is unaffected.

See plan binary-juggling-locket.md → Read-only enforcement.
"""
import os


def _bot_query_mode() -> bool:
    return os.environ.get("BOT_QUERY_MODE") == "1"


class BotQueryRouter:
    def db_for_read(self, model, **hints):
        if _bot_query_mode() and model._meta.app_label == "rollcall":
            return "readonly"
        return None

    def db_for_write(self, model, **hints):
        if _bot_query_mode() and model._meta.app_label == "rollcall":
            # Route the write to readonly — the role can't INSERT/UPDATE/DELETE
            # on rollcall_* tables, so the write fails at Postgres. The router
            # itself can't reject queries; this just denies through the role.
            return "readonly"
        return None

    def allow_relation(self, obj1, obj2, **hints):
        # Don't restrict relations — both default and readonly point at the
        # same database, just with different users.
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Never run migrations against the readonly alias.
        if db == "readonly":
            return False
        return None
