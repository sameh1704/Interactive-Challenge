"""Tests for the credential rate limit.

The application presents two secrets to a client - a Screen ID and a password -
and neither is length-limited anywhere else. These tests pin the behaviour that
makes online guessing impractical, and just as importantly the behaviour that
keeps a legitimate classroom working: a whole room behind one NAT address, and a
screen that reconnects on its own, must not be throttled.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from accounts.roles import Role
from core import ratelimit
from classrooms.models import Classroom
from screens.models import InteractiveScreen


class AllowTests(TestCase):
    """The counter itself."""

    def setUp(self):
        cache.clear()

    def test_attempts_within_the_limit_are_allowed(self):
        for _ in range(3):
            self.assertTrue(
                ratelimit.allow("test", "1.2.3.4", limit=3, window=60),
            )

    def test_attempts_past_the_limit_are_refused(self):
        for _ in range(3):
            ratelimit.allow("test", "1.2.3.4", limit=3, window=60)

        self.assertFalse(ratelimit.allow("test", "1.2.3.4", limit=3, window=60))
        self.assertFalse(ratelimit.allow("test", "1.2.3.4", limit=3, window=60))

    def test_counters_are_separate_per_identifier(self):
        for _ in range(3):
            ratelimit.allow("test", "1.2.3.4", limit=3, window=60)

        # A different address, and a different namespace, are both unaffected.
        self.assertTrue(ratelimit.allow("test", "5.6.7.8", limit=3, window=60))
        self.assertTrue(ratelimit.allow("other", "1.2.3.4", limit=3, window=60))

    def test_the_key_does_not_contain_the_identifier(self):
        """The counter is reachable under the digest, and the input is not in it.

        Read through ``cache.get`` rather than by poking at a backend's internal
        dictionary, so the assertion holds whichever cache the deployment uses.
        """
        identifier = "hunter2-should-not-appear"

        ratelimit.allow("test", identifier, limit=5, window=60)

        # Counted: the attempt is reachable under the hashed key.
        key = ratelimit._digest("test", identifier)
        self.assertEqual(cache.get(key), 1)

        # Not leaked: a caller cannot put chosen text - a password it is
        # guessing, say - into the cache key space.
        self.assertNotIn("hunter2", key)

    def test_the_key_is_a_fixed_length_regardless_of_the_input(self):
        """A long identifier cannot be used to make the key space grow, and two
        identifiers must not collide."""
        prefix = "ratelimit:test:"
        self.assertEqual(len(ratelimit._digest("test", "a")), len(prefix) + 64)
        self.assertEqual(
            len(ratelimit._digest("test", "a")),
            len(ratelimit._digest("test", "x" * 5000)),
        )
        self.assertNotEqual(
            ratelimit._digest("test", "a"),
            ratelimit._digest("test", "b"),
        )

    def test_a_zero_limit_means_unlimited_rather_than_blocked(self):
        """A misconfigured 0 must not lock every user out."""
        for _ in range(50):
            self.assertTrue(ratelimit.allow("test", "1.2.3.4", limit=0, window=60))


class ScreenRateLimitTests(TestCase):
    """The Screen ID is a bearer credential, so presenting one is counted."""

    def setUp(self):
        cache.clear()
        self.classroom = Classroom.objects.create(name="Science Lab A")
        self.screen = InteractiveScreen.objects.create(
            name="Front board", classroom=self.classroom
        )

    @override_settings(SCREEN_RATE_LIMIT=5, SCREEN_RATE_LIMIT_WINDOW=60)
    def test_repeated_guessing_eventually_stops_being_answered(self):
        path = reverse("screens:status")
        valid = self.screen.screen_id

        statuses = [
            self.client.get(path, {"screen_id": valid}).status_code for _ in range(8)
        ]

        self.assertIn(429, statuses, "an unlimited script must eventually be refused")
        # Every status is either the real page or a refusal; nothing reveals
        # whether the Screen ID existed.
        self.assertTrue(set(statuses) <= {200, 429})

    @override_settings(SCREEN_RATE_LIMIT=5, SCREEN_RATE_LIMIT_WINDOW=60)
    def test_the_throttled_response_looks_like_an_unregistered_screen(self):
        path = reverse("screens:status")

        for _ in range(5):
            self.client.get(path, {"screen_id": self.screen.screen_id})
        refused = self.client.get(path, {"screen_id": self.screen.screen_id})

        self.assertEqual(refused.status_code, 429)
        body = refused.content.decode()
        # No Screen ID, classroom or hint is echoed back.
        self.assertNotIn(self.screen.screen_id, body)
        self.assertNotIn(self.classroom.name, body)

    @override_settings(SCREEN_RATE_LIMIT=1000, SCREEN_RATE_LIMIT_WINDOW=60)
    def test_a_whole_classroom_behind_one_address_is_not_throttled(self):
        """Twenty screens on one NAT address must all be served."""
        path = reverse("screens:status")

        for index in range(20):
            screen = InteractiveScreen.objects.create(
                name=f"Board {index}", classroom=self.classroom
            )
            response = self.client.get(path, {"screen_id": screen.screen_id})
            self.assertEqual(response.status_code, 200, f"screen {index} was refused")

    @override_settings(SCREEN_RATE_LIMIT=5, SCREEN_RATE_LIMIT_WINDOW=60)
    def test_the_heartbeat_is_limited_too(self):
        url = reverse("screens:heartbeat")

        for _ in range(5):
            self.client.post(url, {"screen_id": self.screen.screen_id})
        refused = self.client.post(url, {"screen_id": self.screen.screen_id})

        self.assertEqual(refused.status_code, 429)

    @override_settings(SCREEN_RATE_LIMIT=5, SCREEN_RATE_LIMIT_WINDOW=60)
    def test_the_live_screen_page_is_limited_too(self):
        path = reverse("live:screen")

        for _ in range(5):
            self.client.get(path, {"screen_id": self.screen.screen_id})
        refused = self.client.get(path, {"screen_id": self.screen.screen_id})

        self.assertEqual(refused.status_code, 429)

    @override_settings(SCREEN_RATE_LIMIT=5, SCREEN_RATE_LIMIT_WINDOW=47)
    def test_a_refusal_says_how_long_to_wait(self):
        """Without Retry-After a screen on a reconnect backoff has no idea how
        long to pause, and retries straight back into the same refusal."""
        path = reverse("screens:status")

        for _ in range(5):
            self.client.get(path, {"screen_id": self.screen.screen_id})
        refused = self.client.get(path, {"screen_id": self.screen.screen_id})

        self.assertEqual(refused["Retry-After"], "47")

    @override_settings(SCREEN_RATE_LIMIT=5, SCREEN_RATE_LIMIT_WINDOW=47)
    def test_the_heartbeat_refusal_also_says_how_long_to_wait(self):
        url = reverse("screens:heartbeat")

        for _ in range(5):
            self.client.post(url, {"screen_id": self.screen.screen_id})
        refused = self.client.post(url, {"screen_id": self.screen.screen_id})

        self.assertEqual(refused["Retry-After"], "47")

    @override_settings(SCREEN_RATE_LIMIT=5, SCREEN_RATE_LIMIT_WINDOW=60)
    def test_the_throttled_page_is_byte_for_byte_the_unregistered_page(self):
        """The anti-enumeration guarantee is about the *body*. The status and
        Retry-After differ, and neither says anything about the Screen ID."""
        path = reverse("screens:status")

        unknown = self.client.get(path, {"screen_id": "AM-NOPE00"})

        for _ in range(5):
            self.client.get(path, {"screen_id": self.screen.screen_id})
        refused = self.client.get(path, {"screen_id": self.screen.screen_id})

        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(refused.status_code, 429)
        self.assertNotIn("Retry-After", unknown)
        # No Screen ID, classroom name or hint in either body.
        for response in (unknown, refused):
            body = response.content.decode()
            self.assertNotIn(self.screen.screen_id, body)
            self.assertNotIn(self.classroom.name, body)


class ForwardedAddressTests(TestCase):
    """A client must not be able to choose the address it is counted against.

    ``SCREEN_TRUST_FORWARDED_FOR`` exists because in production the request has
    already passed through the project's own proxy, and the proxy's address is
    not the screen's. Two things have to be true at once:

    * while it is **off** (the default, and every development stack) a forged
      header must change nothing - the counter follows ``REMOTE_ADDR``; and
    * while it is **on**, it is only safe because ``deploy/nginx`` *overwrites*
      the header rather than appending to it. That half is asserted in
      ``core.tests.test_deployment_config``, because it is nginx behaviour and no
      Django test can see it.
    """

    def setUp(self):
        cache.clear()
        self.classroom = Classroom.objects.create(name="Science Lab A")
        self.screen = InteractiveScreen.objects.create(
            name="Front board", classroom=self.classroom
        )
        self.url = reverse("screens:status")

    @override_settings(
        SCREEN_RATE_LIMIT=3,
        SCREEN_RATE_LIMIT_WINDOW=60,
        SCREEN_TRUST_FORWARDED_FOR=False,
    )
    def test_a_forged_forwarded_header_does_not_reset_the_counter(self):
        for _ in range(3):
            self.client.get(
                self.url,
                {"screen_id": self.screen.screen_id},
                REMOTE_ADDR="10.0.0.9",
                HTTP_X_FORWARDED_FOR="203.0.113.1",
            )

        # A fresh forged address on the next request must not buy a new budget.
        refused = self.client.get(
            self.url,
            {"screen_id": self.screen.screen_id},
            REMOTE_ADDR="10.0.0.9",
            HTTP_X_FORWARDED_FOR="198.51.100.77",
        )

        self.assertEqual(refused.status_code, 429)

    @override_settings(
        SCREEN_RATE_LIMIT=3,
        SCREEN_RATE_LIMIT_WINDOW=60,
        SCREEN_TRUST_FORWARDED_FOR=False,
    )
    def test_each_real_address_gets_its_own_budget(self):
        """The limiter separates addresses rather than pooling them."""
        for _ in range(3):
            self.client.get(
                self.url, {"screen_id": self.screen.screen_id}, REMOTE_ADDR="10.0.0.9"
            )

        other = self.client.get(
            self.url, {"screen_id": self.screen.screen_id}, REMOTE_ADDR="10.0.0.10"
        )

        self.assertEqual(other.status_code, 200)

    @override_settings(
        SCREEN_RATE_LIMIT=1000,
        SCREEN_RATE_LIMIT_WINDOW=60,
        SCREEN_TRUST_FORWARDED_FOR=False,
    )
    def test_twenty_screens_behind_one_nat_address_are_all_served(self):
        """The realistic classroom case: one public address, many screens.

        Each screen loads its page and then heartbeats on its own interval, so a
        room of twenty produces a steady stream of presentations from a single
        address. None of them may be refused.
        """
        statuses = []
        for index in range(20):
            screen = InteractiveScreen.objects.create(
                name=f"Board {index}", classroom=self.classroom
            )
            statuses.append(
                self.client.get(
                    self.url, {"screen_id": screen.screen_id}, REMOTE_ADDR="10.0.0.9"
                ).status_code
            )
            statuses.append(
                self.client.post(
                    reverse("screens:heartbeat"),
                    {"screen_id": screen.screen_id},
                    REMOTE_ADDR="10.0.0.9",
                ).status_code
            )

        self.assertEqual(set(statuses), {200}, "a screen behind the NAT was refused")


class LoginRateLimitTests(TestCase):
    """A password is the only thing guarding every teacher function."""

    def setUp(self):
        cache.clear()
        self.password = "correct-horse-battery-staple"
        self.user = User.objects.create_user(
            username="teacher.one", password=self.password, role=Role.TEACHER
        )
        self.url = reverse("accounts:login")

    def _attempt(self, username="teacher.one", password="wrong-password"):
        return self.client.post(
            self.url, {"username": username, "password": password}
        )

    @override_settings(LOGIN_RATE_LIMIT=4, LOGIN_RATE_LIMIT_WINDOW=300)
    def test_guessing_a_password_eventually_stops_being_evaluated(self):
        for _ in range(4):
            self._attempt()

        # Rate limiting counts attempts, so even a correct password is refused
        # once the limit is reached. That is the intended trade: the account is
        # protected, and the lockout ends when the window passes.
        response = self._attempt(password=self.password)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(LOGIN_RATE_LIMIT=3, LOGIN_RATE_LIMIT_WINDOW=300)
    def test_a_wrong_password_and_an_unknown_username_cost_the_same(self):
        for _ in range(3):
            self._attempt(username="teacher.one", password="wrong")

        unknown = self._attempt(username="does.not.exist", password="wrong")

        self.assertEqual(unknown.status_code, 200)
        self.assertContains(unknown, "not recognised")

    @override_settings(LOGIN_RATE_LIMIT=5, LOGIN_RATE_LIMIT_WINDOW=300)
    def test_a_correct_sign_in_still_works(self):
        response = self._attempt(password=self.password)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_the_limit_is_configurable_and_generous_by_default(self):
        """A classroom of teachers must never be locked out by the default."""
        self.assertGreaterEqual(settings.LOGIN_RATE_LIMIT, 10)
        self.assertGreaterEqual(settings.SCREEN_RATE_LIMIT, 120)


class LoginQuotaTests(TestCase):
    """The shape of the sign-in quota.

    Two facts are deliberate and worth pinning, because both look like bugs:

    * a **successful** sign-in consumes the quota, exactly as a failed one does.
      The counter is on attempts, because whether an attempt would have succeeded
      is exactly what an attacker controls and what must not change the cost;
    * the quota is **per username**, so a busy staff room signing in as different
      teachers from one address does not lock each other out. The per-address
      ceiling is the per-username limit times ``LOGIN_RATE_LIMIT_ACCOUNT_FACTOR``.
    """

    def setUp(self):
        cache.clear()
        self.password = "correct-horse-battery-staple"
        self.teacher = User.objects.create_user(
            username="teacher.one", password=self.password, role=Role.TEACHER
        )
        self.other = User.objects.create_user(
            username="teacher.two", password=self.password, role=Role.TEACHER
        )
        self.url = reverse("accounts:login")

    def _sign_in(self, username, password=None, address="10.0.0.5"):
        """One sign-in attempt, from a client that is not already signed in.

        A fresh client each time: ``LoginView`` redirects an authenticated visitor
        before the form is ever validated, so reusing one client would measure the
        redirect instead of the limiter.
        """
        return Client().post(
            self.url,
            {"username": username, "password": password or self.password},
            REMOTE_ADDR=address,
        )

    @override_settings(LOGIN_RATE_LIMIT=5, LOGIN_RATE_LIMIT_WINDOW=300)
    def test_a_successful_sign_in_consumes_the_quota(self):
        for _ in range(5):
            self.assertEqual(self._sign_in("teacher.one").status_code, 302)

        # Documented trade: the sixth sign-in inside the window is refused even
        # though the password is right. The lockout ends when the window passes.
        refused = self._sign_in("teacher.one")
        self.assertEqual(refused.status_code, 200)
        self.assertNotIn("_auth_user_id", refused.wsgi_request.session)

    @override_settings(LOGIN_RATE_LIMIT=5, LOGIN_RATE_LIMIT_WINDOW=300)
    def test_the_quota_is_per_username(self):
        """One teacher filling the quota must not lock out a colleague."""
        for _ in range(5):
            self.assertEqual(self._sign_in("teacher.one").status_code, 302)

        response = self._sign_in("teacher.two")

        # The body is in the failure message on purpose: a 200 here could be a
        # refusal or a rejected credential, and those are different bugs.
        self.assertEqual(response.status_code, 302, response.content.decode()[:400])

    @override_settings(LOGIN_RATE_LIMIT=5, LOGIN_RATE_LIMIT_WINDOW=300)
    def test_the_username_actually_reaches_the_counter(self):
        """Guards a real defect.

        ``AuthenticationForm.username_field`` is the model's field *object*, not
        a string, so reading ``self.data`` with it yields "". Every account then
        shares one counter, which caps the entire school at LOGIN_RATE_LIMIT
        sign-ins per window, and leaves the audit log unable to name the account
        that was targeted.
        """
        self._sign_in("teacher.one", password="wrong")
        self._sign_in("teacher.two", password="wrong")

        self.assertEqual(cache.get(ratelimit._digest("login-name", "teacher.one")), 1)
        self.assertEqual(cache.get(ratelimit._digest("login-name", "teacher.two")), 1)

    @override_settings(LOGIN_RATE_LIMIT=2, LOGIN_RATE_LIMIT_WINDOW=300)
    def test_spraying_many_usernames_from_one_address_is_bounded(self):
        """The per-address ceiling catches what the per-username counter cannot.

        Each distinct username gets its own budget, so only the address counter
        can stop one machine cycling through thousands of them.
        """
        factor = settings.LOGIN_RATE_LIMIT_ACCOUNT_FACTOR

        allowed = sum(
            1
            for index in range(factor * 4)
            if ratelimit.login_allowed(f"absent.teacher.{index}", "10.0.0.5")
        )

        self.assertEqual(allowed, 2 * factor)

    @override_settings(LOGIN_RATE_LIMIT=2, LOGIN_RATE_LIMIT_WINDOW=300)
    def test_one_account_guessed_from_many_addresses_is_bounded(self):
        """Conversely: spreading one guessing run over many source addresses must
        not buy more attempts at the account."""
        allowed = sum(
            1
            for index in range(6)
            if ratelimit.login_allowed("teacher.one", f"10.0.0.{index}")
        )

        self.assertEqual(allowed, 2)

    @override_settings(LOGIN_RATE_LIMIT=2, LOGIN_RATE_LIMIT_WINDOW=300)
    def test_the_bound_applies_through_the_real_form(self):
        """The same limit observed the way an attacker would meet it: two wrong
        guesses from two addresses, then the right password from a third."""
        first = self._sign_in("teacher.one", password="wrong", address="10.0.0.1")
        second = self._sign_in("teacher.one", password="wrong", address="10.0.0.2")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        correct = self._sign_in("teacher.one", address="10.0.0.3")

        self.assertEqual(correct.status_code, 200)
        self.assertNotIn("_auth_user_id", correct.wsgi_request.session)
        self.assertContains(correct, "not recognised")

    @override_settings(LOGIN_RATE_LIMIT=10, LOGIN_RATE_LIMIT_WINDOW=300)
    def test_ten_teachers_in_a_staff_room_all_sign_in(self):
        """The realistic worst case at the shipped default: ten different
        accounts, one shared address."""
        for index in range(10):
            username = f"teacher.number{index}"
            User.objects.create_user(
                username=username, password=self.password, role=Role.TEACHER
            )
            self.assertEqual(
                self._sign_in(username).status_code,
                302,
                f"{username} could not sign in from a shared address",
            )
