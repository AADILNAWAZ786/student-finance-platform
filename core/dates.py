"""Business-date helpers.

API requests that do not carry a business date use today's date in the project time
zone (``TIME_ZONE = Asia/Kolkata``). The data generator always passes explicit dates,
so generated data never depends on the wall clock (ARCHITECTURE P4).
"""

from datetime import date

from django.utils import timezone


def business_today() -> date:
    return timezone.localdate()
