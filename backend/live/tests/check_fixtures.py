"""Fixture cleanup for the networked check scripts.

``live_multi_client_check``, ``live_reveal_check`` and ``live_load_check`` all
run against a *real* stack and therefore all write to a real database: a
competition, a teacher account, classrooms, screens and questions. On a
development machine that is untidy. On the school server it is worse, because
these scripts are documented as something an administrator may run against the
running deployment to confirm it works — and a check that leaves three
classrooms, two screens and a teacher behind after every run fills the real
question bank and the real screen list with rows nobody will ever delete.

So cleanup lives here, once, rather than being re-implemented in each script.
Two properties matter and are what this module exists to guarantee:

**Nothing is left behind.** Every row a run creates is removed: answers, the
frozen result, the competition and its join rows, the screens, the classrooms,
the questions, the accounts, and the session rows those accounts created.
Deleting only the competition — which is what the scripts used to do — leaves
every other row behind, because nothing cascades to them.

**Nothing else is touched.** Every lookup is keyed on the run's own unique
``tag``, or on the exact primary key of the competition this run created. A
classroom is matched by ``name__contains=tag`` and an account by
``username__contains=tag``, and the tag is six hex characters minted per run.
There is no code path here that can match a row the school created, so running a
check against production data cannot delete school data.

Deleting a user does **not** delete its sessions: ``django_session`` has no
foreign key to the user, it stores the user id inside an opaque encoded blob.
Sessions are therefore matched by decoding and comparing, which is why
``_sessions_for_users`` exists rather than a cascade being assumed.
"""

from __future__ import annotations


def discard_check_fixtures(*, tag: str, competition_id: int | None = None) -> dict:
    """Remove everything a single check run created. Returns a per-model count.

    Safe to call more than once, and safe to call when the run failed part way
    through and some of the rows were never created.

    ``tag`` must be the same unique value the run stamped onto its fixtures.
    ``competition_id`` is the competition this run created; pass ``None`` only
    when a run failed before creating one, in which case the tag still bounds
    every other lookup.
    """
    from accounts.models import User
    from classrooms.models import Classroom
    from competitions.models import Competition
    from questions.models import Question
    from screens.models import InteractiveScreen
    from scoring.models import Answer, CompetitionResult

    removed: dict[str, int] = {}

    # --- the leaves first: rows other tables protect ---------------------
    #
    # Order is not cosmetic. `Question` is protected by the competition's
    # question list and by every answer, `InteractiveScreen` is protected by its
    # classroom, and `Competition.current_question` protects the question it is
    # currently showing. Deleting top-down makes Django refuse the delete.
    if competition_id is not None:
        removed["Answer"] = Answer.objects.filter(
            competition_id=competition_id
        ).delete()[0]
        removed["CompetitionResult"] = CompetitionResult.objects.filter(
            competition_id=competition_id
        ).delete()[0]
        # Clear the pointer first: it is a PROTECTed reference and a finished
        # round may still be pointing at the question it ended on.
        Competition.objects.filter(pk=competition_id).update(current_question=None)
        removed["Competition"] = Competition.objects.filter(
            pk=competition_id
        ).delete()[0]

    removed["InteractiveScreen"] = InteractiveScreen.objects.filter(
        name__contains=tag
    ).delete()[0]
    removed["Classroom"] = Classroom.objects.filter(name__contains=tag).delete()[0]
    removed["Question"] = Question.objects.filter(text__contains=tag).delete()[0]

    # --- accounts, and the sessions they left behind ---------------------
    accounts = User.objects.filter(username__contains=tag)
    user_ids = {str(pk) for pk in accounts.values_list("pk", flat=True)}
    removed["Session"] = _delete_sessions_for(user_ids) if user_ids else 0
    removed["User"] = accounts.delete()[0]

    return removed


def _delete_sessions_for(user_ids: set[str]) -> int:
    """Delete the session rows belonging to ``user_ids``.

    ``django_session`` has no foreign key to the user, so there is no cascade to
    rely on: the user id is inside the encoded ``session_data`` blob. Decoding
    every session is acceptable here because a check run leaves a handful, and
    the alternative — leaving an orphaned session that still authenticates if the
    session key is known — is worse than a short loop.
    """
    from django.contrib.sessions.backends.db import SessionStore
    from django.contrib.sessions.models import Session

    doomed = []
    for session in Session.objects.only("session_key", "session_data"):
        try:
            data = SessionStore().decode(session.session_data)
        except Exception:  # noqa: BLE001 - an undecodable row is not ours to keep
            continue
        if str(data.get("_auth_user_id", "")) in user_ids:
            doomed.append(session.session_key)

    if not doomed:
        return 0
    return Session.objects.filter(session_key__in=doomed).delete()[0]


def assert_no_residue(*, tag: str, competition_id: int | None = None) -> dict:
    """Return whatever this tag still matches. An empty dict is the pass case.

    Used by the check scripts to *prove* cleanup happened rather than assume it,
    so a regression in the purge above is reported instead of quietly leaving
    rows behind for the next run to trip over.
    """
    from accounts.models import User
    from classrooms.models import Classroom
    from questions.models import Question
    from screens.models import InteractiveScreen
    from competitions.models import Competition

    residue = {}
    for label, queryset in (
        ("users", User.objects.filter(username__contains=tag)),
        ("classrooms", Classroom.objects.filter(name__contains=tag)),
        ("questions", Question.objects.filter(text__contains=tag)),
        ("screens", InteractiveScreen.objects.filter(name__contains=tag)),
    ):
        count = queryset.count()
        if count:
            residue[label] = count

    if competition_id is not None:
        count = Competition.objects.filter(pk=competition_id).count()
        if count:
            residue["competitions"] = count

    return residue


__all__ = ["assert_no_residue", "discard_check_fixtures"]