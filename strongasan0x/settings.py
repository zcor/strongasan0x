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

# AI Provider API Keys
OPENAI_API_KEY = config("OPENAI_API_KEY", default="")
ANTHROPIC_API_KEY = config("ANTHROPIC_API_KEY", default="")
GROK_API_KEY = config("GROK_API_KEY", default="")
DEEPSEEK_API_KEY = config("DEEPSEEK_API_KEY", default="")

# Strava API Configuration
STRAVA_CLIENT_ID = config("STRAVA_CLIENT_ID", default="")
STRAVA_CLIENT_SECRET = config("STRAVA_CLIENT_SECRET", default="")
