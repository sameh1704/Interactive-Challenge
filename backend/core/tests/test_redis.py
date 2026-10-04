"""Redis is wired into Django as cache backend and Channels layer transport."""

from __future__ import annotations

import os
import unittest

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase


class ChannelsConfigurationTests(SimpleTestCase):
    def test_asgi_application_is_configured(self) -> None:
        self.assertEqual(settings.ASGI_APPLICATION, "config.asgi.application")

    def test_channel_layer_uses_redis(self) -> None:
        channel_layer = settings.CHANNEL_LAYERS["default"]

        self.assertEqual(
            channel_layer["BACKEND"], "channels_redis.core.RedisChannelLayer"
        )

    def test_channel_layer_host_matches_the_redis_host(self) -> None:
        hosts = settings.CHANNEL_LAYERS["default"]["CONFIG"]["hosts"]
        expected_host = os.environ.get("REDIS_HOST", "")
        expected_port = os.environ.get("REDIS_PORT", "6379")

        self.assertEqual(len(hosts), 1)
        # A host may be a plain URL or a kwargs dict carrying connection options
        # such as `socket_timeout`, so compare against whichever form is used.
        address = hosts[0]["address"] if isinstance(hosts[0], dict) else hosts[0]
        self.assertIn(expected_host, address)
        self.assertIn(expected_port, address)

    def test_websocket_routing_table_is_importable(self) -> None:
        from config.routing import websocket_urlpatterns

        self.assertIsInstance(websocket_urlpatterns, list)

    def test_asgi_application_builds(self) -> None:
        """Importing the ASGI application must not raise."""
        from config.asgi import application
        from channels.routing import ProtocolTypeRouter

        self.assertIsInstance(application, ProtocolTypeRouter)


class CacheConfigurationTests(SimpleTestCase):
    def test_cache_backend_is_redis(self) -> None:
        self.assertEqual(
            settings.CACHES["default"]["BACKEND"],
            "django.core.cache.backends.redis.RedisCache",
        )

    def test_cache_location_matches_the_redis_host(self) -> None:
        location = settings.CACHES["default"]["LOCATION"]

        self.assertIn(os.environ.get("REDIS_HOST", ""), location)
        self.assertIn(os.environ.get("REDIS_PORT", "6379"), location)

    def test_redis_password_is_never_baked_into_settings(self) -> None:
        """An unset password must not leak a literal into the configuration."""
        if not os.environ.get("REDIS_PASSWORD"):
            self.assertNotIn("@", settings.CACHES["default"]["LOCATION"])


def _redis_is_reachable() -> bool:
    try:
        cache.set("core:tests:probe", "1", timeout=5)
        return cache.get("core:tests:probe") == "1"
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


@unittest.skipUnless(
    _redis_is_reachable(), "Redis is not reachable from the test process."
)
class RedisConnectivityTests(TestCase):
    """These tests require a live Redis server, as provided by Docker Compose."""

    def test_cache_round_trip_succeeds(self) -> None:
        cache.set("core:tests:round-trip", "value", timeout=5)

        self.assertEqual(cache.get("core:tests:round-trip"), "value")

    def test_cache_delete_succeeds(self) -> None:
        cache.set("core:tests:delete", "value", timeout=5)
        cache.delete("core:tests:delete")

        self.assertIsNone(cache.get("core:tests:delete"))

    def test_channel_layer_resolves_to_the_redis_transport(self) -> None:
        """Proves the Channels layer is wired to the Redis backend, not InMemory."""
        from channels.layers import get_channel_layer
        from channels_redis.core import RedisChannelLayer

        layer = get_channel_layer()

        self.assertIsInstance(layer, RedisChannelLayer)

    def test_channel_layer_round_trips_a_group_message(self) -> None:
        """Exercises the exact mechanism live rounds will depend on.

        Channels 4 removed BaseChannelLayer.groups_for, so the group state is
        proved by an end-to-end publish instead: join a group, broadcast to it,
        and receive the message on the member channel.
        """
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        group = "core.tests.group"
        channel = async_to_sync(layer.new_channel)()

        async_to_sync(layer.group_add)(group, channel)
        try:
            async_to_sync(layer.group_send)(
                group, {"type": "test.message", "text": "round-trip"}
            )
            message = async_to_sync(layer.receive)(channel)
        finally:
            async_to_sync(layer.group_discard)(group, channel)

        self.assertEqual(message["type"], "test.message")
        self.assertEqual(message["text"], "round-trip")