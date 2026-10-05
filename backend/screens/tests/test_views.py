"""Screen identification and the heartbeat endpoint.

Also verifies what a screen may and may not reach: a screen is an unauthenticated
device, so the endpoint surface must stay deliberately narrow.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.roles import Role
from classrooms.models import Classroom
from core.tests.utils import create_administrator, create_teacher
from screens.models import InteractiveScreen


class ScreenIdentificationTests(TestCase):
    def setUp(self) -> None:
        self.classroom = Classroom.objects.create(name="Science Lab 1", grade="Grade 7")
        self.screen = InteractiveScreen.objects.create(
            name="Front interactive board", classroom=self.classroom
        )

    def test_a_screen_identifies_itself_with_its_screen_id(self) -> None:
        response = self.client.get(
            reverse("screens:status"), {"screen_id": self.screen.screen_id}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "screens/status.html")

    def test_the_page_shows_the_screen_and_its_classroom(self) -> None:
        response = self.client.get(
            reverse("screens:status"), {"screen_id": self.screen.screen_id}
        )

        self.assertContains(response, "Front interactive board")
        self.assertContains(response, self.classroom.display_name)
        self.assertContains(response, self.screen.screen_id)

    def test_a_screen_can_identify_its_classroom(self) -> None:
        response = self.client.get(
            reverse("screens:status"), {"screen_id": self.screen.screen_id}
        )

        self.assertEqual(response.context["classroom"], self.classroom)
        self.assertEqual(response.context["screen"], self.screen)

    def test_an_unknown_screen_id_is_refused(self) -> None:
        response = self.client.get(reverse("screens:status"), {"screen_id": "AM-ZZZZZZ"})

        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "screens/unregistered.html")

    def test_a_missing_screen_id_is_refused(self) -> None:
        self.assertEqual(self.client.get(reverse("screens:status")).status_code, 404)

    def test_a_retired_screen_is_refused(self) -> None:
        self.screen.active = False
        self.screen.save(update_fields=["active"])

        response = self.client.get(
            reverse("screens:status"), {"screen_id": self.screen.screen_id}
        )

        self.assertEqual(response.status_code, 404)

    def test_an_unassigned_screen_still_identifies_itself(self) -> None:
        spare = InteractiveScreen.objects.create(name="Spare board")

        response = self.client.get(
            reverse("screens:status"), {"screen_id": spare.screen_id}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not yet assigned to a classroom")

    def test_an_unknown_and_a_retired_screen_look_identical(self) -> None:
        """The endpoint must not reveal which Screen IDs exist."""
        retired = InteractiveScreen.objects.create(name="Retired", active=False)
        unknown = self.client.get(reverse("screens:status"), {"screen_id": "AM-ZZZZZZ"})
        known = self.client.get(
            reverse("screens:status"), {"screen_id": retired.screen_id}
        )

        self.assertEqual(unknown.status_code, known.status_code)
        self.assertTemplateUsed(unknown, "screens/unregistered.html")
        self.assertTemplateUsed(known, "screens/unregistered.html")

    def test_the_screen_id_is_matched_case_insensitively(self) -> None:
        response = self.client.get(
            reverse("screens:status"),
            {"screen_id": self.screen.screen_id.lower()},
        )

        self.assertEqual(response.status_code, 200)

    def test_no_sign_in_is_required_to_open_the_screen_page(self) -> None:
        response = self.client.get(
            reverse("screens:status"), {"screen_id": self.screen.screen_id}
        )

        self.assertEqual(response.status_code, 200)


class ScreenPageDoesNotLeakAdministrationTests(TestCase):
    """A screen must not be able to reach staff or other screens."""

    def setUp(self) -> None:
        # A distinctive username: the unregistered page legitimately contains the
        # ordinary English word "administrator", which would otherwise collide
        # with the default factory username and pass as a false positive.
        self.administrator = create_administrator(username="chief.admin")
        self.teacher = create_teacher(username="instructor.two")
        self.classroom = Classroom.objects.create(name="Science Lab 1")
        self.screen = InteractiveScreen.objects.create(
            name="Front board", classroom=self.classroom
        )
        self.other_screen = InteractiveScreen.objects.create(
            name="Secret board", classroom=self.classroom
        )

    def _status_response(self):
        return self.client.get(
            reverse("screens:status"), {"screen_id": self.screen.screen_id}
        )

    def test_no_other_screen_is_named_on_the_page(self) -> None:
        self.assertNotContains(self._status_response(), "Secret board")
        self.assertNotContains(self._status_response(), self.other_screen.screen_id)

    def test_no_staff_account_is_named_on_the_page(self) -> None:
        response = self._status_response()

        self.assertNotContains(response, self.administrator.username)
        self.assertNotContains(response, self.teacher.username)

    def test_no_administration_link_is_offered(self) -> None:
        response = self._status_response()

        self.assertNotContains(response, "/admin/")
        self.assertNotContains(response, reverse("screens:heartbeat") + "?screen_id=")

    def test_the_unregistered_page_leaks_nothing_either(self) -> None:
        response = self.client.get(reverse("screens:status"), {"screen_id": "AM-ZZZZZZ"})

        # This page is a 404, so the "not present" check has to allow that status.
        self.assertNotContains(response, self.screen.screen_id, status_code=404)
        self.assertNotContains(response, self.administrator.username, status_code=404)

    def test_a_screen_cannot_reach_the_admin_site(self) -> None:
        response = self.client.get("/admin/screens/interactivescreen/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])


class HeartbeatEndpointTests(TestCase):
    def setUp(self) -> None:
        self.classroom = Classroom.objects.create(name="Science Lab 1")
        self.screen = InteractiveScreen.objects.create(
            name="Front board", classroom=self.classroom
        )

    def _post(self, screen_id: str | None = None, data: dict | None = None, **environ):
        """POST a heartbeat.

        ``data`` becomes part of the submitted form body (used to prove that a
        screen cannot smuggle extra fields). Keyword arguments become WSGI
        environ values, which is how the test client simulates the network and
        browser the screen is really running on.
        """
        payload = {"screen_id": screen_id or self.screen.screen_id}
        payload.update(data or {})
        return self.client.post(reverse("screens:heartbeat"), payload, **environ)

    def test_a_heartbeat_returns_the_screen_status(self) -> None:
        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["registered"], True)
        self.assertEqual(response.json()["screen_id"], self.screen.screen_id)
        self.assertEqual(response.json()["status"], "online")

    def test_a_heartbeat_records_last_seen(self) -> None:
        self.assertIsNone(self.screen.last_seen)

        self._post()

        self.screen.refresh_from_db()
        self.assertIsNotNone(self.screen.last_seen)

    def test_a_heartbeat_records_the_observed_address(self) -> None:
        self._post(REMOTE_ADDR="10.0.0.7")

        self.screen.refresh_from_db()
        self.assertEqual(self.screen.ip_address, "10.0.0.7")

    def test_a_heartbeat_records_the_observed_browser(self) -> None:
        self._post(HTTP_USER_AGENT="Mozilla/5.0 (X11; Linux x86_64)")

        self.screen.refresh_from_db()
        self.assertEqual(self.screen.browser_user_agent, "Mozilla/5.0 (X11; Linux x86_64)")

    def test_a_heartbeat_reports_the_classroom_to_the_screen(self) -> None:
        self.assertEqual(self._post().json()["classroom"], self.classroom.display_name)

    def test_an_unknown_screen_id_is_refused(self) -> None:
        response = self._post(screen_id="AM-ZZZZZZ")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["registered"], False)

    def test_a_retired_screen_is_refused(self) -> None:
        self.screen.active = False
        self.screen.save(update_fields=["active"])

        self.assertEqual(self._post().status_code, 404)

    def test_a_heartbeat_requires_a_post(self) -> None:
        response = self.client.get(reverse("screens:heartbeat"))

        self.assertEqual(response.status_code, 405)

    def test_a_heartbeat_cannot_change_the_screen_identity_or_classroom(self) -> None:
        """A screen reports presence only; it cannot reconfigure itself."""
        original_id = self.screen.screen_id
        elsewhere = Classroom.objects.create(name="Library")

        self._post(
            data={
                "name": "Renamed by the screen",
                "classroom": elsewhere.pk,
                "active": "false",
                "is_primary": "true",
                "last_seen": "1999-01-01T00:00:00Z",
            },
        )

        self.screen.refresh_from_db()
        self.assertEqual(self.screen.screen_id, original_id)
        self.assertEqual(self.screen.name, "Front board")
        self.assertEqual(self.screen.classroom, self.classroom)
        self.assertTrue(self.screen.active)
        self.assertFalse(self.screen.is_primary)

    @override_settings(SCREEN_TRUST_FORWARDED_FOR=True)
    def test_forwarded_address_is_used_when_explicitly_trusted(self) -> None:
        self._post(
            REMOTE_ADDR="10.0.0.7",
            HTTP_X_FORWARDED_FOR="203.0.113.9, 10.0.0.1",
        )

        self.screen.refresh_from_db()
        self.assertEqual(self.screen.ip_address, "203.0.113.9")

    @override_settings(SCREEN_TRUST_FORWARDED_FOR=False)
    def test_forwarded_address_is_ignored_when_not_trusted(self) -> None:
        """An attacker can set X-Forwarded-For, so it must not be believed."""
        self._post(REMOTE_ADDR="10.0.0.7", HTTP_X_FORWARDED_FOR="1.2.3.4")

        self.screen.refresh_from_db()
        self.assertEqual(self.screen.ip_address, "10.0.0.7")

    @override_settings(SCREEN_TRUST_FORWARDED_FOR=True)
    def test_an_untrustworthy_forwarded_address_is_discarded(self) -> None:
        self._post(REMOTE_ADDR="10.0.0.7", HTTP_X_FORWARDED_FOR="not-an-address")

        self.screen.refresh_from_db()
        self.assertEqual(self.screen.ip_address, "10.0.0.7")

    @override_settings(SCREEN_ONLINE_WINDOW_SECONDS=90)
    def test_repeated_heartbeats_keep_the_screen_online(self) -> None:
        self.screen.record_heartbeat(
            seen_at=timezone.now() - timedelta(seconds=120)
        )
        self.screen.refresh_from_db()
        self.assertEqual(self.screen.status, "offline")

        self._post()

        self.screen.refresh_from_db()
        self.assertEqual(self.screen.status, "online")

    def test_the_heartbeat_response_exposes_no_staff_or_other_screens(self) -> None:
        administrator = create_administrator(username="chief.admin")
        other = InteractiveScreen.objects.create(name="Secret board")

        body = self._post().content.decode()

        self.assertNotIn(administrator.username, body)
        self.assertNotIn(other.screen_id, body)
        self.assertNotIn("Secret board", body)


@override_settings(
    CACHES={
        # `core.ratelimit` counts through the configured cache. Left on the
        # default it would be Redis, which in a deployed stack is the live one:
        # these tests would write counters into production, `cache.clear()` would
        # flush production, and one test's counter would throttle every later
        # test in the class. A local memory cache keeps the class hermetic while
        # exercising exactly the same rate-limit code path.
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
)
class HeartbeatCsrfEnforcedTests(TestCase):
    """The heartbeat under a test client that actually enforces CSRF.

    Every other heartbeat test in this module uses the default client, whose
    ``enforce_csrf_checks`` is ``False``. That bypasses ``CsrfViewMiddleware``
    entirely, so those tests pass whether or not a real screen could ever obtain
    a token - which is exactly how the heartbeat shipped broken while the suite
    stayed green.

    A real interactive screen is anonymous and stays anonymous: it never signs
    in and never will. The token therefore has to be reachable from a plain page
    load. The client here is built with ``enforce_csrf_checks=True`` so the
    middleware runs, and the tests below are written the way the browser on a
    classroom screen behaves: load the page, read the ``csrftoken`` cookie, send
    it back in the ``X-CSRFToken`` header.
    """

    def setUp(self) -> None:
        # `LocMemCache` keys its storage by cache name in a module-level dict, so
        # a closed cache is not a cleared one: without this, the flood the
        # rate-limit tests below deliberately cause would throttle every later
        # test in the class. Local memory only, so production is never touched.
        cache.clear()

        self.classroom = Classroom.objects.create(name="Science Lab 1")
        self.screen = InteractiveScreen.objects.create(
            name="Front board", classroom=self.classroom
        )
        # A separate, CSRF-enforcing client. Deliberately not `self.client`: the
        # rest of the module must keep testing the view, not the middleware.
        self.csrf_client = Client(enforce_csrf_checks=True)

    # -- helpers ---------------------------------------------------------

    def _load_page(self, screen_id: str | None = None):
        """Open the screen page and return its response.

        This is what sets the `csrftoken` cookie. A screen does exactly this and
        nothing else before it first reports presence.
        """
        return self.csrf_client.get(
            reverse("screens:status"),
            {"screen_id": screen_id or self.screen.screen_id},
        )

    def _token(self) -> str:
        """The masked token the browser would read out of the cookie jar.

        `csrf_client.cookies` is the same jar `fetch(..., {credentials:
        "same-origin"})` would send back, so this is the value
        `screen-heartbeat.js` actually reads with `readCookie("csrftoken")`.
        """
        return self.csrf_client.cookies["csrftoken"].value

    def _heartbeat(self, **environ):
        return self.csrf_client.post(
            reverse("screens:heartbeat"),
            {"screen_id": self.screen.screen_id},
            **environ,
        )

    # -- the cookie is issued to an anonymous caller ---------------------

    def test_an_anonymous_screen_page_sets_a_csrf_cookie(self) -> None:
        """The precondition for everything else: a screen can obtain a token.

        Without this the heartbeat is unreachable from a real screen, because
        `CsrfViewMiddleware` has nothing to compare a submitted token against.
        """
        response = self._load_page()

        self.assertEqual(response.status_code, 200)
        self.assertIn("csrftoken", response.cookies)
        self.assertIn("csrftoken", self.csrf_client.cookies)

    def test_the_live_screen_page_also_sets_a_csrf_cookie(self) -> None:
        """Both screen pages render the shared base template, so both must.

        A screen left open on the live page is the one that reports presence
        during a competition.
        """
        response = self.csrf_client.get(
            reverse("live:screen"), {"screen_id": self.screen.screen_id}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("csrftoken", response.cookies)

    # -- a valid token is accepted ---------------------------------------

    def test_a_heartbeat_with_the_page_token_is_accepted(self) -> None:
        """The production path: load the page, send the cookie value back."""
        self._load_page()

        response = self._heartbeat(HTTP_X_CSRFTOKEN=self._token())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["registered"], True)
        self.assertEqual(response.json()["screen_id"], self.screen.screen_id)

    def test_a_heartbeat_with_the_token_updates_last_seen(self) -> None:
        """The bug this class exists for: presence must actually be recorded."""
        self._load_page()
        self.assertIsNone(self.screen.last_seen)

        self._heartbeat(HTTP_X_CSRFTOKEN=self._token())

        self.screen.refresh_from_db()
        self.assertIsNotNone(self.screen.last_seen)
        self.assertTrue(self.screen.is_online)

    def test_the_browser_style_header_is_accepted(self) -> None:
        """`screen-heartbeat.js` sends the header, not a form field.

        This is the only form the classroom screen actually uses, so it is the
        test that would have caught the shipped defect.
        """
        self._load_page()

        response = self._heartbeat(HTTP_X_CSRFTOKEN=self._token())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "online")

    def test_the_token_is_accepted_as_a_form_field_too(self) -> None:
        """The documented Django alternative, kept working for a plain form."""
        self._load_page()

        response = self.csrf_client.post(
            reverse("screens:heartbeat"),
            {
                "screen_id": self.screen.screen_id,
                "csrfmiddlewaretoken": self._token(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["registered"], True)

    def test_repeated_heartbeats_from_one_open_page_all_succeed(self) -> None:
        """The screen reloads once and then reports presence every 30 seconds.

        The token stays valid across that page's lifetime, so the second and
        third beats must not be rejected.
        """
        self._load_page()
        token = self._token()

        for _ in range(3):
            response = self._heartbeat(HTTP_X_CSRFTOKEN=token)
            self.assertEqual(response.status_code, 200)

    # -- a missing or wrong token is still refused ------------------------

    def test_a_heartbeat_without_any_token_is_forbidden(self) -> None:
        """The protection this fix must not have removed."""
        response = self._heartbeat()

        self.assertEqual(response.status_code, 403)
        self.screen.refresh_from_db()
        self.assertIsNone(self.screen.last_seen)

    def test_a_heartbeat_without_a_token_is_forbidden_even_after_a_page_load(self) -> None:
        """Holding the cookie is not enough; the token must be presented.

        Without this, a caller who had once loaded any page would bypass CSRF
        entirely by simply omitting the header.
        """
        self._load_page()

        response = self._heartbeat()

        self.assertEqual(response.status_code, 403)

    def test_a_forged_token_is_forbidden(self) -> None:
        self._load_page()

        response = self._heartbeat(HTTP_X_CSRFTOKEN="x" * 64)

        self.assertEqual(response.status_code, 403)

    def test_a_token_from_a_different_session_is_forbidden(self) -> None:
        """A token is only good to the browser it was issued to.

        This models the cross-site case: another site has its own valid token,
        and presenting it here must not be accepted.
        """
        self._load_page()
        someone_else = Client(enforce_csrf_checks=True)
        someone_else.get(reverse("screens:status"),
                         {"screen_id": self.screen.screen_id})
        their_token = someone_else.cookies["csrftoken"].value

        response = self._heartbeat(HTTP_X_CSRFTOKEN=their_token)

        self.assertEqual(response.status_code, 403)

    def test_an_empty_token_is_forbidden(self) -> None:
        """Exactly what `screen-heartbeat.js` sent when the cookie was missing.

        `readCookie()` returned null, `|| ""` made it an empty string, and that
        header is what produced the 403 in production.
        """
        self._load_page()

        response = self._heartbeat(HTTP_X_CSRFTOKEN="")

        self.assertEqual(response.status_code, 403)

    # -- fixing the cookie must not weaken the rest of the surface --------

    def test_an_invalid_screen_id_with_a_valid_token_is_a_404(self) -> None:
        """CSRF is checked first, then the Screen ID. Both must still apply.

        A 404 rather than a 403 proves the request passed CSRF validation and was
        then refused by the view on its own terms, which is what keeps the
        anti-enumeration guarantee intact.
        """
        self._load_page()

        response = self.csrf_client.post(
            reverse("screens:heartbeat"),
            {"screen_id": "AM-ZZZZZZ"},
            HTTP_X_CSRFTOKEN=self._token(),
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["registered"], False)

    def test_an_unknown_screen_id_without_a_token_is_forbidden_not_a_404(self) -> None:
        """CSRF runs before the lookup, so an unauthenticated prober learns nothing.

        The status has to be identical for every unknown ID whether or not the
        caller held a token, or the difference itself becomes a signal.
        """
        self._load_page()

        without_token = self.csrf_client.post(
            reverse("screens:heartbeat"), {"screen_id": "AM-ZZZZZZ"}
        )
        self.assertEqual(without_token.status_code, 403)

        other_unknown = self.csrf_client.post(
            reverse("screens:heartbeat"), {"screen_id": "AM-YYYYYY"},
            HTTP_X_CSRFTOKEN=self._token(),
        )
        self.assertEqual(other_unknown.status_code, 404)

    def test_a_retired_screen_with_a_valid_token_is_a_404(self) -> None:
        self._load_page()
        self.screen.active = False
        self.screen.save(update_fields=["active"])

        response = self._heartbeat(HTTP_X_CSRFTOKEN=self._token())

        self.assertEqual(response.status_code, 404)

    def test_the_heartbeat_remains_rate_limited_with_csrf_enforced(self) -> None:
        """The token fix must not become a way around the Screen ID limiter.

        Every request here carries a valid token, so the only thing that can stop
        the flood is `core.ratelimit`.
        """
        from django.conf import settings
        from django.core.cache import cache

        self._load_page()
        token = self._token()
        cache.clear()

        limit = settings.SCREEN_RATE_LIMIT
        for _ in range(limit):
            self._heartbeat(HTTP_X_CSRFTOKEN=token)
        response = self._heartbeat(HTTP_X_CSRFTOKEN=token)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"],
                         str(settings.SCREEN_RATE_LIMIT_WINDOW))
        self.assertEqual(response.json(), {
            "registered": False,
            "detail": "This screen is not registered.",
        })

    def test_the_page_load_itself_remains_rate_limited(self) -> None:
        """Presentation is counted on the page as well as the heartbeat."""
        from django.conf import settings
        from django.core.cache import cache

        cache.clear()
        for _ in range(settings.SCREEN_RATE_LIMIT):
            self._load_page()
        response = self._load_page()

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"],
                         str(settings.SCREEN_RATE_LIMIT_WINDOW))

    def test_a_signed_in_user_can_still_sign_out(self) -> None:
        """The existing sign-out form keeps its own token.

        The new hidden form adds a second token input to every authenticated
        page. This proves it did not displace the one that form depends on:
        both are present, and logout still works.
        """
        teacher = create_teacher(username="csrf.teacher")
        self.csrf_client.force_login(teacher)

        page = self.csrf_client.get(reverse("dashboard"))
        self.assertEqual(page.status_code, 200)
        # Two token inputs: the new hidden one and the sign-out form's own.
        self.assertContains(page, 'name="csrfmiddlewaretoken"', count=2)
        self.assertContains(page, reverse("accounts:logout"))

        response = self.csrf_client.post(
            reverse("accounts:logout"),
            {"csrfmiddlewaretoken": self.csrf_client.cookies["csrftoken"].value},
        )
        self.assertEqual(response.status_code, 302)

        self.assertEqual(self.csrf_client.get(reverse("dashboard")).status_code, 302)

    def test_the_hidden_token_form_does_not_leak_the_heartbeat_url(self) -> None:
        """The token form has no action, so it cannot become a posted form."""
        response = self._load_page()

        self.assertNotContains(response, reverse("screens:heartbeat"))


class ScreenUrlTests(TestCase):
    def test_the_screen_page_is_mounted_at_the_documented_path(self) -> None:
        self.assertEqual(reverse("screens:status"), "/screen/")

    def test_the_heartbeat_endpoint_is_under_the_screen_path(self) -> None:
        self.assertEqual(reverse("screens:heartbeat"), "/screen/heartbeat/")