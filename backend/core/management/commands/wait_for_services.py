"""``wait_for_services``: block until PostgreSQL and Redis are usable.

Runs as the first step of the container entrypoint so that the web container
does not start serving, and does not start running migrations, while its
dependencies are still booting. ``depends_on`` health checks cover most of the
race, but a restart of an individual dependency can still leave a brief window
that this command absorbs.
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from core.health import check_database, check_redis

CHECKS = (("database", check_database), ("redis", check_redis))


class Command(BaseCommand):
    help = "Wait until the configured database and Redis accept connections."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--timeout",
            type=float,
            default=60.0,
            help="Maximum number of seconds to wait (default: 60).",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=1.0,
            help="Seconds between attempts (default: 1).",
        )

    def handle(self, *args, **options) -> None:
        timeout: float = options["timeout"]
        interval: float = options["interval"]
        deadline = time.monotonic() + timeout
        reported: set[str] = set()

        while True:
            pending = {name for name, check in CHECKS if check()["status"] != "ok"}
            for name in sorted(pending - reported):
                self.stdout.write(f"Waiting for {name}...")
                reported.add(name)

            if not pending:
                self.stdout.write(self.style.SUCCESS("All services are ready."))
                return

            if time.monotonic() >= deadline:
                raise CommandError(
                    f"Timed out after {timeout:g}s waiting for: "
                    f"{', '.join(sorted(pending))}."
                )

            time.sleep(interval)