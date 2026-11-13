from rest_framework.routers import SimpleRouter

from scholarships.views import EligibilityViewSet, ScholarshipTypeViewSet, ScholarshipViewSet

router = SimpleRouter()
router.register("scholarship-types", ScholarshipTypeViewSet, basename="scholarship-type")
router.register("eligibility", EligibilityViewSet, basename="eligibility")
router.register("scholarships", ScholarshipViewSet, basename="scholarship")
urlpatterns = router.urls
