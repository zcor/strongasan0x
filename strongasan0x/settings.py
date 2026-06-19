"""
Django settings for strongasan0x project.

Weekly fitness contest platform with AI-powered rankings.
"""

from pathlib import Path
from decouple import config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config("SECRET_KEY")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config("DEBUG", default=False, cast=bool)

# Require explicit ALLOWED_HOSTS - no wildcard default
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1").split(",")


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rollcall",
    "daily",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "strongasan0x.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "strongasan0x.wsgi.application"


# Database
# https://docs.djangoproject.com/en/5.0/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME", default="rollcall"),
        "USER": config("DB_USER", default="rollcall_user"),
        "PASSWORD": config("DB_PASSWORD", default=""),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default="5432"),
    }
}

# Read-only DB alias for the conversational bot's bot_query verb dispatcher.
# Backed by a Postgres role with SELECT-only grants on rollcall_* tables.
# See plan binary-juggling-locket.md → Read-only enforcement.
# Falls back to the default user when no separate password is configured —
# in that case the router still routes here, but writes are not blocked at
# the Postgres layer (the boundary collapses to the verb dispatcher only).
BOT_READONLY_DB_USER = config("BOT_READONLY_DB_USER", default="")
BOT_READONLY_DB_PASSWORD = config("BOT_READONLY_DB_PASSWORD", default="")
DATABASES["readonly"] = {
    **DATABASES["default"],
    "USER": BOT_READONLY_DB_USER or DATABASES["default"]["USER"],
    "PASSWORD": BOT_READONLY_DB_PASSWORD or DATABASES["default"]["PASSWORD"],
}

# Router engages only when BOT_QUERY_MODE=1 in env (set by bot_query mgmt cmd).
# Otherwise it's a no-op — normal Django/web/management code is unaffected.
DATABASE_ROUTERS = ["rollcall.db_routers.BotQueryRouter"]


# Password validation
# https://docs.djangoproject.com/en/5.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.0/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "US/Pacific"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.0/howto/static-files/

STATIC_URL = "static/"

# Default primary key field type
# https://docs.djangoproject.com/en/5.0/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Login URL for admin-only views
LOGIN_URL = '/admin/login/'

# Discord Bot Configuration
DISCORD_BOT_TOKEN = config("DISCORD_BOT_TOKEN", default="")
DISCORD_GUILD_ID = config("DISCORD_GUILD_ID", default="")
DISCORD_ATTESTATION_CHANNEL_ID = config("DISCORD_ATTESTATION_CHANNEL_ID", default="")
DISCORD_TOP_10_ROLE_NAME = config("DISCORD_TOP_10_ROLE_NAME", default="Top Ten")
DISCORD_TOP_5_ROLE_NAME = config("DISCORD_TOP_5_ROLE_NAME", default="Top 5")
DISCORD_ADMIN_ROLE_NAME = config("DISCORD_ADMIN_ROLE_NAME", default="Admin")
DISCORD_ADMIN_CHANNEL_ID = config("DISCORD_ADMIN_CHANNEL_ID", default="")
DISCORD_TOP_10_CHANNEL_ID = config("DISCORD_TOP_10_CHANNEL_ID", default="")
DISCORD_TOP_5_CHANNEL_ID = config("DISCORD_TOP_5_CHANNEL_ID", default="")

# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN = config("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_BOT_USERNAME = config("TELEGRAM_BOT_USERNAME", default="")  # For Login Widget
TELEGRAM_WEBHOOK_URL = config("TELEGRAM_WEBHOOK_URL", default="")
TELEGRAM_WEBHOOK_SECRET = config("TELEGRAM_WEBHOOK_SECRET", default="")
TELEGRAM_WEBHOOK_PORT = config("TELEGRAM_WEBHOOK_PORT", default=8443, cast=int)
TELEGRAM_ATTESTATION_CHANNEL_ID = config("TELEGRAM_ATTESTATION_CHANNEL_ID", default="")

# Attestation Detection Settings
ATTESTATION_MIN_LENGTH = config("ATTESTATION_MIN_LENGTH", default=100, cast=int)
ATTESTATION_WEEKEND_START_HOUR = config("ATTESTATION_WEEKEND_START_HOUR", default=17, cast=int)
ATTESTATION_MULTI_PART_WINDOW_MINUTES = config("ATTESTATION_MULTI_PART_WINDOW_MINUTES", default=15, cast=int)

# Conversational bot upgrade (binary-juggling-locket plan)
# Phase A — Sonnet classifier replaces heuristic detector. Verdicts always logged
# to MessageLog.classifier_verdict; this flag controls whether they drive the
# attestation pipeline. DRY_RUN_ATTESTATIONS shadow-tests without storing.
CONVERSATION_CLASSIFIER_ENABLED = config("CONVERSATION_CLASSIFIER_ENABLED", default=False, cast=bool)
CONVERSATION_DRY_RUN_ATTESTATIONS = config("CONVERSATION_DRY_RUN_ATTESTATIONS", default=False, cast=bool)
CONVERSATION_CLASSIFIER_MODEL = config("CONVERSATION_CLASSIFIER_MODEL", default="claude-sonnet-4-6")
CONVERSATION_CACHE_TTL = config("CONVERSATION_CACHE_TTL", default="1h")
# Phase B — DM-only conversational replies via Claude CLI subprocess.
CONVERSATION_REPLIES_ENABLED = config("CONVERSATION_REPLIES_ENABLED", default=False, cast=bool)
CONVERSATION_REPLIES_DM_ONLY = config("CONVERSATION_REPLIES_DM_ONLY", default=True, cast=bool)
CONVERSATION_CLAUDE_CLI_PATH = config("CONVERSATION_CLAUDE_CLI_PATH", default="claude")
CONVERSATION_CODEX_CLI_PATH = config("CONVERSATION_CODEX_CLI_PATH", default="codex")
CONVERSATION_CLAUDE_TIMEOUT_SEC = config("CONVERSATION_CLAUDE_TIMEOUT_SEC", default=90, cast=int)
CONVERSATION_GLOBAL_CONCURRENCY = config("CONVERSATION_GLOBAL_CONCURRENCY", default=2, cast=int)
CONVERSATION_PER_CHAT_COOLDOWN_SEC = config("CONVERSATION_PER_CHAT_COOLDOWN_SEC", default=8, cast=int)
CONVERSATION_AMBIENT_PER_CHAT_PER_HOUR = config("CONVERSATION_AMBIENT_PER_CHAT_PER_HOUR", default=2, cast=int)
CONVERSATION_CLAUDE_CIRCUIT_HOURS = config("CONVERSATION_CLAUDE_CIRCUIT_HOURS", default=6, cast=int)
CONVERSATION_HISTORY_TURNS = config("CONVERSATION_HISTORY_TURNS", default=20, cast=int)

# AI Provider API Keys
OPENAI_API_KEY = config("OPENAI_API_KEY", default="")
ANTHROPIC_API_KEY = config("ANTHROPIC_API_KEY", default="")
GROK_API_KEY = config("GROK_API_KEY", default="")
DEEPSEEK_API_KEY = config("DEEPSEEK_API_KEY", default="")

# X (Twitter) API Configuration
X_CONSUMER_KEY = config("X_CONSUMER_KEY", default="")
X_CONSUMER_SECRET = config("X_CONSUMER_SECRET", default="")
X_ACCESS_TOKEN = config("X_ACCESS_TOKEN", default="")
X_ACCESS_TOKEN_SECRET = config("X_ACCESS_TOKEN_SECRET", default="")
X_BEARER_TOKEN = config("X_BEARER_TOKEN", default="")

# Strava API Configuration
STRAVA_CLIENT_ID = config("STRAVA_CLIENT_ID", default="")
STRAVA_CLIENT_SECRET = config("STRAVA_CLIENT_SECRET", default="")

# Web Push (daily/ home-screen badge). Public key is exposed to the browser
# (it's meant to be); private key is a secret set in .env on the server only.
# VAPID_SUBJECT is a contact mailto/url required by the push spec.
VAPID_PUBLIC_KEY = config("VAPID_PUBLIC_KEY", default="")
VAPID_PRIVATE_KEY = config("VAPID_PRIVATE_KEY", default="")
VAPID_SUBJECT = config("VAPID_SUBJECT", default="mailto:curvedefi@gmail.com")
