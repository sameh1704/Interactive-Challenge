from django.db import models  # noqa: F401

# This app derives every figure from data owned by competitions, scoring and
# 	ournaments. It deliberately has no models of its own: a report that kept
# numbers would be a second source of truth, able to disagree with the records it
# was derived from.