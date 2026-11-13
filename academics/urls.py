from rest_framework.routers import SimpleRouter

from academics.views import EnrollmentViewSet, ProgramViewSet, TermViewSet

router = SimpleRouter()
router.register("programs", ProgramViewSet, basename="program")
router.register("terms", TermViewSet, basename="term")
router.register("enrollments", EnrollmentViewSet, basename="enrollment")
urlpatterns = router.urls
