"""Tests for the production deployment files.

These are configuration, not code, but two of them carry the security property
the whole Phase 8 hardening rests on, and neither is visible from a Python
import:

* **The proxy must overwrite ``X-Forwarded-For``.** Production turns on
  ``SCREEN_TRUST_FORWARDED_FOR`` (docker-compose.prod.yml), and
  ``screens.network.client_ip`` then reads the client address out of that header.
  If the proxy *appends* to the header with ``$proxy_add_x_forwarded_for``, a
  client can send its own left-most value, be counted under a fresh address on
  every request, and walk straight through the Screen ID rate limit. Nothing in
  the Django test suite can catch that, because the Django test client does not
  run nginx. Asserting it here is the only place it can be caught.
* **Only the proxy may publish a host port.** If ``challenge-web`` regains a
  published port, the application can be reached without passing the proxy, and
  the header above is forgeable again.

The rest pin the deployment facts Phase 8 depends on: production settings are the
ones actually loaded, no secret is written into a committed file, and the
persistent volumes exist.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

#: Where the deployment files live. Normally backend/'s parent, which is the source
#: checkout. ``DEPLOYMENT_ROOT`` overrides it so the same tests can run inside the
#: container image, which carries only backend/ and tests/:
#:
#:     docker cp deploy docker-compose.yml docker-compose.prod.yml .env.example \
#:         almanar-challenge-web:/tmp/deployment/
#:     docker compose exec -T -e DEPLOYMENT_ROOT=/tmp/deployment challenge-web \
#:         python manage.py test core.tests.test_deployment_config
PROJECT_ROOT = Path(os.environ.get("DEPLOYMENT_ROOT") or Path(settings.BASE_DIR).parent)

NGINX_TEMPLATE = (
    PROJECT_ROOT / "deploy" / "nginx" / "templates" / "challenge.conf.template"
)
PROD_COMPOSE = PROJECT_ROOT / "docker-compose.prod.yml"
DEV_COMPOSE = PROJECT_ROOT / "docker-compose.yml"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

#: The settings module's own source. Always present, in a source checkout and
#: inside the container image alike, because the image copies `backend/` to /app.
#: Used to assert what a setting's *shipped default* is, which `settings` cannot
#: answer: by the time settings are built the environment has already overridden
#: whatever the default was.
BASE_SETTINGS = Path(settings.BASE_DIR) / "config" / "settings" / "base.py"

#: The container image carries only backend/ and tests/, so these files exist in a
#: source checkout and not inside challenge-web. The tests skip rather than fail
#: there: the host checkout is where these files are edited and reviewed.
deployment_files_present = unittest.skipUnless(
    NGINX_TEMPLATE.exists() and PROD_COMPOSE.exists() and ENV_EXAMPLE.exists(),
    "deployment files are not present (running outside a source checkout)",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def uncommented(path: Path) -> str:
    """The file with its comment lines removed, for literal-content assertions."""
    return "\n".join(
        line for line in read(path).splitlines() if not line.strip().startswith("#")
    )


@deployment_files_present
class ProxyHeaderTests(SimpleTestCase):
    """The proxy must not pass a client's own forwarding claim through."""

    def setUp(self):
        self.nginx = read(NGINX_TEMPLATE)

    def test_the_proxy_overwrites_the_forwarded_for_header(self):
        self.assertRegex(
            self.nginx,
            r"proxy_set_header\s+X-Forwarded-For\s+\$remote_addr\s*;",
            "the proxy must replace X-Forwarded-For with the peer address, so a "
            "client cannot choose the address it is counted against",
        )

    def test_the_proxy_never_appends_to_the_forwarded_for_header(self):
        """``$proxy_add_x_forwarded_for`` is the exact defect this replaces.

        It keeps whatever the client sent in the left-most position, which is the
        position ``client_ip`` reads. Checked against the configuration only -
        the comment explaining why it is not used names it deliberately.
        """
        self.assertNotIn(
            "$proxy_add_x_forwarded_for",
            uncommented(NGINX_TEMPLATE),
            "appending to a client-supplied X-Forwarded-For makes the Screen ID "
            "rate limit bypassable",
        )

    def test_the_forwarded_host_and_proto_are_also_overwritten(self):
        for header in ("X-Forwarded-Host", "X-Forwarded-Proto"):
            self.assertRegex(
                self.nginx,
                rf"proxy_set_header\s+{header}\s+\$(host|scheme)\s*;",
                f"{header} must be set by the proxy, not taken from the client",
            )

    def test_production_trusting_the_header_is_consistent_with_overwriting_it(self):
        """The coupling, pinned from both sides.

        ``SCREEN_TRUST_FORWARDED_FOR=True`` is only safe because the proxy
        overwrites. If either half changes, this fails rather than leaving the
        combination silently unsafe.
        """
        self.assertIn(
            "SCREEN_TRUST_FORWARDED_FOR: ${SCREEN_TRUST_FORWARDED_FOR:-True}",
            read(PROD_COMPOSE),
        )
        self.assertRegex(
            read(NGINX_TEMPLATE),
            r"proxy_set_header\s+X-Forwarded-For\s+\$remote_addr\s*;",
        )

    def test_the_proxy_config_sets_no_school_address(self):
        """Nothing about the target network is baked into the shipped config."""
        body = uncommented(NGINX_TEMPLATE)
        for address in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", body):
            self.assertIn(
                address,
                {"127.0.0.1", "127.0.0.11"},
                f"{address} is a literal address in the proxy config; the "
                "deployment's address belongs in .env",
            )


@deployment_files_present
class NginxStructureTests(SimpleTestCase):
    """The dynamic-DNS design is deliberate and must not be tidied away."""

    def setUp(self):
        self.nginx = read(NGINX_TEMPLATE)

    def test_the_upstream_is_named_through_a_variable(self):
        """A literal upstream block resolves once, at startup.

        Naming it in an ``upstream`` block means nginx keeps dialling the address
        challenge-web had when it started, and answers 502 after every redeploy
        until somebody restarts the proxy by hand. A variable forces a per-request
        re-resolution through the resolver below.
        """
        self.assertRegex(
            self.nginx, r"set\s+\$challenge_upstream\s+http://challenge-web:8000\s*;"
        )
        self.assertRegex(self.nginx, r"proxy_pass\s+\$challenge_upstream\s*;")
        self.assertNotRegex(self.nginx, r"proxy_pass\s+http://challenge-web:8000\s*;")

    def test_there_is_no_upstream_block(self):
        self.assertNotRegex(
            self.nginx,
            r"^\s*upstream\s+\w+\s*\{",
            "an upstream block cannot be used while proxy_pass names the host "
            "through a variable, and keeping one invites that mistake",
        )

    def test_a_resolver_is_declared_for_the_variable_upstream(self):
        self.assertRegex(self.nginx, r"resolver\s+127\.0\.0\.11\s+valid=\d+s")

    def test_the_websocket_upgrade_headers_are_present(self):
        """Without these the handshake is answered as ordinary HTTP."""
        self.assertRegex(self.nginx, r"proxy_set_header\s+Upgrade\s+\$http_upgrade\s*;")
        self.assertRegex(
            self.nginx, r"proxy_set_header\s+Connection\s+\$connection_upgrade\s*;"
        )
        self.assertIn("proxy_buffering off;", self.nginx)

    def test_the_server_token_is_suppressed(self):
        """The proxy must not advertise its exact version.

        `Server: nginx/1.27.5` on every response tells anything that can reach
        the port which nginx build to check published advisories against.
        `server_tokens off` reduces the header to plain `nginx`.
        """
        self.assertRegex(
            self.nginx,
            r"(?m)^\s*server_tokens\s+off\s*;",
            "the proxy template must set `server_tokens off;`",
        )

    def test_the_server_token_suppression_is_not_undone(self):
        """Guards against a later edit quietly re-exposing the version."""
        self.assertNotRegex(
            self.nginx,
            r"(?m)^\s*server_tokens\s+on\s*;",
            "`server_tokens on` would put the version back in the header",
        )

    def test_the_socket_read_timeout_exceeds_the_application_heartbeat(self):
        """A round can sit idle between questions; the socket must survive it.

        The value is a template placeholder, so the shipped **default** is what
        has to be checked - that is what a deployment gets without tuning.
        """
        body = uncommented(NGINX_TEMPLATE)
        self.assertRegex(body, r"proxy_read_timeout\s+\$\{NGINX_PROXY_READ_TIMEOUT\};")
        self.assertRegex(body, r"proxy_send_timeout\s+\$\{NGINX_PROXY_SEND_TIMEOUT\};")

        defaults = {
            match.group(1): int(match.group(2))
            for match in re.finditer(
                r"(NGINX_PROXY_(?:READ|SEND)_TIMEOUT):\s*\$\{[^:}]+:?-?(\d+)s\}",
                read(PROD_COMPOSE),
            )
        }
        self.assertTrue(defaults, "the socket timeouts have no shipped default")
        for name, value in defaults.items():
            self.assertGreater(
                value,
                settings.LIVE_HEARTBEAT_SECONDS,
                f"{name}={value}s would close a socket the application is still "
                "using to keep it alive",
            )

    def test_every_substituted_variable_is_provided_by_the_compose_environment(self):
        """The image's envsubst leaves unknown names as a literal ``${NAME}``.

        Checked against the configuration only: the comments describe the
        mechanism using ``${...}`` deliberately.
        """
        referenced = set(re.findall(r"\$\{([A-Z0-9_]+)\}", uncommented(NGINX_TEMPLATE)))
        compose = read(PROD_COMPOSE)
        missing = sorted(name for name in referenced if name not in compose)
        self.assertEqual(
            missing,
            [],
            "the nginx template substitutes names the compose environment never "
            "sets, so they would reach nginx unsubstituted",
        )


@deployment_files_present
class ProductionComposeTests(SimpleTestCase):
    """The production overlay must be self-contained and secret-free."""

    def setUp(self):
        self.prod = read(PROD_COMPOSE)

    def test_the_application_container_publishes_no_host_port(self):
        """The proxy is the only way in; that is what makes its headers trusted."""
        self.assertIn(
            "ports: !reset []",
            self.prod,
            "challenge-web must have no published port, or a client can bypass "
            "the proxy and forge X-Forwarded-For",
        )

    def test_only_the_proxy_publishes_a_port(self):
        self.assertRegex(
            self.prod, r"\$\{PROXY_BIND_ADDRESS:-[^}]+\}:\$\{PROXY_PORT:-80\}:80"
        )

    def test_production_settings_are_the_module_that_is_loaded(self):
        self.assertIn(
            "DJANGO_SETTINGS_MODULE: config.settings.production",
            self.prod,
            "both the module and the resolved name are needed: the base compose "
            "file interpolates the module from DJANGO_SETTINGS_MODULE_NAME",
        )
        self.assertIn("DJANGO_SETTINGS_MODULE_NAME: production", self.prod)

    def test_no_secret_is_written_into_the_compose_file(self):
        """Every credential arrives from the environment."""
        for name in ("DJANGO_SECRET_KEY", "DATABASE_PASSWORD", "REDIS_PASSWORD"):
            self.assertIn(f"{name}: ${{{name}", self.prod)
        # A bare literal after the colon would be a committed credential.
        for name in ("DJANGO_SECRET_KEY", "DATABASE_PASSWORD"):
            self.assertRegex(
                self.prod,
                rf"{name}:\s*\$\{{",
                f"{name} must be interpolated, never literal",
            )

    def test_the_required_credentials_are_mandatory(self):
        for name in ("DJANGO_SECRET_KEY", "DATABASE_PASSWORD", "ALLOWED_HOSTS"):
            self.assertIn(f"${{{name}:?{name} is required}}", self.prod)

    def test_migrations_run_once_in_a_prepare_service_not_in_web(self):
        self.assertIn("challenge-prepare:", self.prod)
        self.assertIn("python manage.py migrate --noinput", self.prod)
        self.assertIn('restart: "no"', self.prod)

    def test_collectstatic_runs_with_the_production_module(self):
        """Production uses a manifest storage backend, so the image's
        development-time collectstatic leaves every static file unresolvable."""
        prepare = self.prod.split("challenge-prepare:", 1)[1]
        self.assertIn("python manage.py collectstatic --noinput --clear", prepare)
        self.assertIn("DJANGO_SETTINGS_MODULE_NAME: production", prepare)

    def test_the_datastores_are_referenced_by_service_name_not_by_address(self):
        self.assertIn("DATABASE_HOST: challenge-db", self.prod)
        self.assertIn("REDIS_HOST: challenge-redis", self.prod)
        self.assertNotRegex(self.prod, r"(DATABASE_HOST|REDIS_HOST):\s*[\"']?\d")

    def test_every_long_running_service_restarts_unless_stopped(self):
        for service in ("challenge-proxy", "challenge-web"):
            block = self.prod.split(f"{service}:", 1)[1]
            self.assertIn(
                "restart: unless-stopped",
                block,
                f"{service} must come back after a reboot",
            )

    def test_the_shared_static_volume_is_declared(self):
        self.assertIn("challenge-static:", self.prod)

    def test_the_database_and_redis_volumes_come_from_the_base_compose_file(self):
        """They are already correct; duplicating them here would be a second place
        for them to drift, and this pins that decision."""
        dev = read(DEV_COMPOSE)
        self.assertIn("challenge-db-data:", dev)
        self.assertIn("challenge-redis-data:", dev)
        self.assertIn("challenge-media:", dev)

    def test_the_proxy_healthcheck_asks_the_proxy_not_the_application(self):
        self.assertIn("/proxy-health", self.prod)

    def test_the_websocket_frame_limits_are_bounded(self):
        """Daphne defaults its frame and message limits to 0, meaning no limit."""
        self.assertIn("--websocket-max-message-size", self.prod)
        self.assertIn("--websocket-max-frame-size", self.prod)


@deployment_files_present
class EnvExampleTests(SimpleTestCase):
    """``.env.example`` is what an operator copies, so it must not lie."""

    def setUp(self):
        self.env = read(ENV_EXAMPLE)

    def test_no_secret_value_is_present(self):
        secret = re.search(r"^DJANGO_SECRET_KEY=(.*)$", self.env, re.M)
        self.assertIsNotNone(secret)
        self.assertEqual(
            secret.group(1).strip(),
            "",
            "the example file must ship an empty secret, never a placeholder "
            "that looks usable",
        )

    def test_the_documented_defaults_match_the_settings_defaults(self):
        """The documented numbers are the numbers the code uses."""
        documented = dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(\S*)$", self.env, re.M))
        for name, actual in (
            ("SCREEN_RATE_LIMIT", settings.SCREEN_RATE_LIMIT),
            ("SCREEN_RATE_LIMIT_WINDOW", settings.SCREEN_RATE_LIMIT_WINDOW),
            ("LOGIN_RATE_LIMIT", settings.LOGIN_RATE_LIMIT),
            ("LOGIN_RATE_LIMIT_WINDOW", settings.LOGIN_RATE_LIMIT_WINDOW),
            (
                "LOGIN_RATE_LIMIT_ACCOUNT_FACTOR",
                settings.LOGIN_RATE_LIMIT_ACCOUNT_FACTOR,
            ),
            ("CHANNEL_LAYER_EXPIRY", settings.CHANNEL_LAYER_EXPIRY_SECONDS),
            ("SCREEN_ONLINE_WINDOW_SECONDS", settings.SCREEN_ONLINE_WINDOW_SECONDS),
            (
                "SCREEN_HEARTBEAT_INTERVAL_SECONDS",
                settings.SCREEN_HEARTBEAT_INTERVAL_SECONDS,
            ),
            ("LIVE_HEARTBEAT_SECONDS", settings.LIVE_HEARTBEAT_SECONDS),
        ):
            self.assertIn(name, documented, f"{name} is undocumented")
            self.assertEqual(
                int(documented[name]),
                int(actual),
                f"{name}: .env.example says {documented[name]}, settings use {actual}",
            )

    def test_the_channel_layer_expiry_outlasts_the_heartbeat(self):
        """Otherwise an idle screen's subscription lapses between beats.

        A screen that is silently unsubscribed keeps showing the last question it
        was sent, with nothing on the board to say it is stale.
        """
        self.assertGreater(
            settings.CHANNEL_LAYER_EXPIRY_SECONDS,
            settings.LIVE_HEARTBEAT_SECONDS,
        )
        self.assertGreater(settings.SCREEN_ONLINE_WINDOW_SECONDS, 60)

    def test_the_base_default_for_forwarded_trust_is_off(self):
        """Trusting the header is only correct behind the overwriting proxy, so
        it is never the default - the production overlay switches it on.

        Asserted against the *source* rather than ``settings``, deliberately.
        This module runs both outside a source checkout (where the deployment
        files are absent and the whole class is skipped) and inside the
        production container, where ``SCREEN_TRUST_FORWARDED_FOR`` is correctly
        ``True``. Reading ``settings`` here made the assertion depend on which
        settings module happened to be loaded, so the one test guarding this
        coupling failed in production - the environment it exists to describe.

        What is actually being guaranteed is unchanged: the shipped default is
        off, so an application run without the proxy behind it never believes a
        client-supplied address. ``test_the_production_overlay_turns_it_on`` in
        ProxyHeaderTests pins the other half.
        """
        source = read(BASE_SETTINGS)
        match = re.search(
            r"SCREEN_TRUST_FORWARDED_FOR\s*=\s*env_bool\(\s*"
            r'"SCREEN_TRUST_FORWARDED_FOR"\s*,\s*default=(\w+)\s*\)',
            source,
        )
        self.assertIsNotNone(
            match, "base.py must read SCREEN_TRUST_FORWARDED_FOR with an explicit default"
        )
        self.assertEqual(
            match.group(1),
            "False",
            "base.py must default SCREEN_TRUST_FORWARDED_FOR to False",
        )
        self.assertRegex(
            self.env, r"(?m)^SCREEN_TRUST_FORWARDED_FOR=(False|false|0|no)\s*$"
        )

    def test_the_proxy_and_websocket_settings_are_documented(self):
        """The effective proxy configuration must be visible in the repository.

        These are read by `docker-compose.prod.yml` and by nothing else, so an
        operator looking at `.env.example` had no way to know they existed, let
        alone what they were set to. Documented here with the values the overlay
        actually defaults to, so the file cannot quietly fall behind the compose
        file it mirrors.
        """
        documented = dict(
            re.findall(r"^([A-Z][A-Z0-9_]*)=(\S*)\s*$", self.env, re.M)
        )
        expected = {
            name: default
            for name, default in re.findall(
                r"\$\{("
                r"NGINX_[A-Z_]+|PROXY_[A-Z_]+|WS_[A-Z_]+):-([^}]+)\}",
                read(PROD_COMPOSE),
            )
        }
        self.assertTrue(
            expected,
            "no proxy or WebSocket variable found in the production overlay",
        )
        for name, default in expected.items():
            self.assertIn(
                name, documented, f"{name} is undocumented in .env.example"
            )
            self.assertEqual(
                documented[name],
                default,
                f"{name}: .env.example says {documented[name]!r}, the compose "
                f"overlay defaults to {default!r}",
            )

    def test_the_documented_session_lifetime_matches_the_settings(self):
        documented = dict(
            re.findall(r"^([A-Z][A-Z0-9_]*)=(\S*)\s*$", self.env, re.M)
        )
        self.assertIn("SESSION_COOKIE_AGE", documented)
        self.assertEqual(
            int(documented["SESSION_COOKIE_AGE"]),
            settings.SESSION_COOKIE_AGE,
        )
