"""Django settings for the Student Finance backend (ARCHITECTURE.md §2, §8).

Two SQLite databases:
  default       db.sqlite3            domain data (everything except ground truth)
  ground_truth  ground_truth.sqlite3  GroundTruthLabel only (§12.2), routed by
                                      config.routers.GroundTruthRouter
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me-for-any-shared-deployment"
)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third party
    "rest_framework",
    "rest_framework.authtoken",
    "django_filters",
    "drf_spectacular",
    # project apps (domain models arrive in Phase 1+)
    "core",
    "students",
    "academics",
    "billing",
    "scholarships",
    "payments",
    "reconciliation",
    "datagen",
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

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- Databases (ARCHITECTURE §8.1) -------------------------------------------
# transaction_mode / init_command require Django >= 5.1. select_for_update() is a
# no-op on SQLite; IMMEDIATE serializes write transactions instead (§8.2).
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
            "init_command": (
                "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;"
            ),
        },
        # file-based test DB so WAL and (later) concurrency tests behave as in production
        "TEST": {"NAME": BASE_DIR / "test_db.sqlite3"},
    },
    "ground_truth": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "ground_truth.sqlite3",
        "OPTIONS": {"transaction_mode": "IMMEDIATE", "timeout": 20},
        "TEST": {"NAME": BASE_DIR / "test_ground_truth.sqlite3"},
    },
}
DATABASE_ROUTERS = ["config.routers.GroundTruthRouter"]

# Verified on every manage.py command by core.checks (ARCHITECTURE §8.2). Declared here,
# in config, because core must not name the ground-truth database itself (§12.2).
REQUIRED_DATABASE_ALIASES = ("default", "ground_truth")
REQUIRED_DATABASE_ROUTERS = ("config.routers.GroundTruthRouter",)
SQLITE_MIN_VERSION = (3, 38, 0)  # JSONField needs the built-in JSON functions

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Students (DATABASE_DESIGN §3) --------------------------------------------
# Student emails must use a domain reserved for documentation, so no real person can
# ever be addressed (lawful provenance, R-53). example.edu is IANA-reserved.
STUDENT_EMAIL_DOMAINS = ("example.edu",)

# --- Money (ARCHITECTURE §2, D6) ---------------------------------------------
CURRENCY = "INR"
MONEY_DECIMAL_PLACES = 2
MONEY_MAX_DIGITS = 12

# --- Django REST Framework ---------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "core.api.StandardPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "EXCEPTION_HANDLER": "core.api.exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "COERCE_DECIMAL_TO_STRING": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Student Finance API",
    "DESCRIPTION": (
        "Tuition and fees, scholarship eligibility, payments, refunds and balance "
        "reconciliation (YottaFlex challenge). Money is exchanged as strings with 2 "
        "decimal places, in INR. Errors use the envelope "
        "`{error: {code, rule_id, message, details}}`."
    ),
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    # several models have a "status" field with different choices; name each enum explicitly
    "ENUM_NAME_OVERRIDES": {
        "StudentStatusEnum": "students.models.Student.Status",
        "EnrollmentStatusEnum": "academics.models.Enrollment.Status",
        "HealthStatusEnum": ["ok", "degraded"],
        "DatabaseHealthStatusEnum": ["ok", "error"],
        # Phase 2 (D9): fee_type and residency have different choice sets per model
        "FeeTypeEnum": "billing.models.FeeSchedule.FeeType",
        "InvoiceLineFeeTypeEnum": "billing.models.InvoiceLine.FeeType",
        "StudentResidencyEnum": "students.models.Student.Residency",
        "FeeScheduleResidencyEnum": "billing.models.FeeSchedule.Residency",
        # Phase 3: scholarship award status
        "ScholarshipStatusEnum": "scholarships.models.Scholarship.Status",
        # Phase 4: payment / refund status and refund reason; keep Phase 3's allocation enum name
        "AllocationTypeEnum": "scholarships.models.ScholarshipAllocation.AllocationType",
        "PaymentAllocationTypeEnum": "payments.models.PaymentAllocation.AllocationType",
        "PaymentStatusEnum": "payments.models.Payment.Status",
        "RefundStatusEnum": "payments.models.Refund.Status",
        "RefundReasonEnum": "payments.models.Refund.Reason",
        "RefundRequestReasonEnum": ["OVERPAYMENT"],
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
}
