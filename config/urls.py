"""Root URL configuration (ARCHITECTURE.md §9).

/api/v1/            versioned API: health, students, programs, terms, enrollments,
                    fee schedules, invoices, billing batches, student ledger/statement/balance,
                    scholarship types, eligibility, scholarship awards
/api/schema/        OpenAPI 3 schema (drf-spectacular)
/api/docs/          Swagger UI
/api/redoc/         ReDoc
/admin/             Django admin
"""

from django.contrib import admin
from django.urls import include, path, re_path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from config.health import HealthView
from core.api import api_not_found

api_v1 = [
    path("health/", HealthView.as_view(), name="health"),
    path("", include("students.urls")),
    path("", include("academics.urls")),
    path("", include("billing.urls")),
    path("", include("scholarships.urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include((api_v1, "api"), namespace="v1")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    # must stay last: any other /api/... URL gets the JSON 404 envelope, not an HTML page
    re_path(r"^api/", api_not_found, name="api-not-found"),
]
