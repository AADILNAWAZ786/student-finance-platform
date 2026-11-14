from rest_framework.routers import SimpleRouter

from datagen.views import GenerationRunViewSet

router = SimpleRouter()
router.register("datagen/runs", GenerationRunViewSet, basename="datagen-run")
urlpatterns = router.urls
