"""The Question model.

Scope for this phase
--------------------
A question now covers four kinds of interaction:

``multiple_choice``
    Pick one of several options. Keyed by :attr:`Question.correct_option`.
``true_false``
    The statement is true or false. Keyed by :attr:`Question.correct_answer`.
``ordering``
    Put several items into the right sequence. Keyed by ``correct_order`` in
    :attr:`Question.type_config`.
``classification``
    Place each item into a named category. Keyed by ``assignments`` in
    :attr:`Question.type_config`.
``short_answer``
    Type an answer the teacher has defined. Keyed by ``accepted_answers`` in
    :attr:`Question.type_config`.

How the types are stored
------------------------
The three original types keep the columns they always had. ``options``,
``correct_option`` and ``correct_answer`` are untouched, so every existing
question keeps working and no data is rewritten.

The newer types are described by a single ``type_config`` JSON column rather
than by a new column each. That is the extensible part: a future question type
adds a key here and a branch in this module, and no migration is needed to add
the storage for it. The alternative - a table per type - would mean a join on
every read of a question that is sent to every screen in the building.

One column also means one place to validate, and :meth:`Question.clean` is that
place: it dispatches on ``question_type`` and refuses a question whose stored
data does not match its type. A question that cannot be scored is worse than no
question at all, because it looks identical from the teacher's side until a
classroom is waiting on an answer that will never come.

The answer key
--------------
Whatever the type, the key lives on the server until answering closes. It is
never part of :meth:`Question.as_live_payload`, which is the only thing a screen
receives while a question is open. See ``scoring.services.submit_answer``.

Difficulty, tags, revision history and question-bank browsing are deliberately
absent. Adding them is a later phase and none of scoring depends on them.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from questions.normalisation import (
    collapse_whitespace,
    is_blank,
    normalise_short_answer,
)

# A question stays on screen long enough for a class to read and answer it, but
# not so long that attention drifts. Used when a competition does not override
# the duration for a particular question.
DEFAULT_DURATION_SECONDS = 30

# Guard against a teacher leaving a question open all afternoon. The live engine
# treats duration as authoritative, so an absurd value would be a real problem.
MAX_DURATION_SECONDS = 600

# Bounds on the question types stored in `type_config`. These are not arbitrary:
# they keep a question readable from the back of a classroom, and they bound the
# length of the canonical answer key so it fits the column `Answer.correct_answer`
# stores for reports.
MIN_ORDERING_ITEMS = 2
MAX_ORDERING_ITEMS = 10
MIN_CLASSIFICATION_ITEMS = 2
MAX_CLASSIFICATION_ITEMS = 12
MIN_CATEGORIES = 2
MAX_CATEGORIES = 6
MIN_ACCEPTED_ANSWERS = 1
MAX_ACCEPTED_ANSWERS = 10

# Longest single item, category or accepted answer. Bounds the stored key.
MAX_ITEM_LENGTH = 60
MAX_CATEGORY_LENGTH = 40
MAX_ACCEPTED_ANSWER_LENGTH = 200

# Image types a question may carry. Django's ImageField already checks that the
# bytes really are an image; this is the second gate, on the extension, so that a
# file that is never rendered as an image cannot be stored and served at all.
ALLOWED_QUESTION_IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "gif", "webp")

validate_question_image = FileExtensionValidator(
    allowed_extensions=ALLOWED_QUESTION_IMAGE_EXTENSIONS,
    message=(
        "Upload a JPEG, PNG, GIF or WebP image. Other file types are not "
        "accepted for a question."
    ),
)


class QuestionType(models.TextChoices):
    """How a question is presented on a screen.

    ``true_false`` needs no options. ``multiple_choice`` needs between two and
    six, which keeps a question readable from the back of a classroom. The
    remaining three describe themselves in ``type_config``.
    """

    MULTIPLE_CHOICE = "multiple_choice", _("Multiple choice")
    TRUE_FALSE = "true_false", _("True or false")
    ORDERING = "ordering", _("Ordering")
    CLASSIFICATION = "classification", _("Classification")
    SHORT_ANSWER = "short_answer", _("Short answer")


# Types whose key lives in the original columns. Everything else is described by
# `type_config`. Referenced by validation and by the live payload, so the two
# cannot disagree about which is which.
LEGACY_KEY_TYPES = frozenset(
    {QuestionType.MULTIPLE_CHOICE, QuestionType.TRUE_FALSE}
)

# The types that need `type_config` to say anything at all.
CONFIGURED_TYPES = frozenset(
    {QuestionType.ORDERING, QuestionType.CLASSIFICATION, QuestionType.SHORT_ANSWER}
)


class QuestionQuerySet(models.QuerySet):
    def usable(self):
        """Questions an operator is allowed to put in a competition."""
        return self.filter(active=True)


class Question(models.Model):
    """A single question that can be asked during a competition."""

    text = models.TextField(
        help_text="The question as it should appear on a screen.",
    )
    question_type = models.CharField(
        max_length=32,
        choices=QuestionType.choices,
        default=QuestionType.MULTIPLE_CHOICE,
        help_text="Determines how a screen renders the question.",
    )
    options = models.JSONField(
        default=list,
        blank=True,
        help_text="Answer options in display order. Empty for true/false.",
    )
    correct_option = models.CharField(
        max_length=255,
        blank=True,
        help_text=(
            "For a multiple choice question, the option text that is correct. "
            "Must match one of the options exactly."
        ),
    )
    correct_answer = models.BooleanField(
        null=True,
        blank=True,
        help_text="For a true/false question, whether the statement is true.",
    )
    image = models.ImageField(
        upload_to="questions/",
        blank=True,
        null=True,
        validators=[validate_question_image],
        help_text="Optional diagram or photograph shown with the question.",
    )
    type_config = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Per-type settings for ordering, classification and short answer "
            "questions. Unused by multiple choice and true/false."
        ),
    )
    explanation = models.TextField(
        blank=True,
        help_text=(
            "Optional explanation shown after the result is revealed. Never sent "
            "to a screen while the question is open."
        ),
    )
    duration_seconds = models.PositiveIntegerField(
        default=DEFAULT_DURATION_SECONDS,
        help_text=(
            "Default time allowed to answer, in seconds. A competition may "
            "override this for a particular question."
        ),
    )
    active = models.BooleanField(
        default=True,
        help_text="Inactive questions are hidden from selection but keep history.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = QuestionQuerySet.as_manager()

    class Meta:
        verbose_name = "question"
        verbose_name_plural = "questions"
        ordering = ["created_at", "pk"]

    def __str__(self) -> str:
        preview = self.text if len(self.text) <= 60 else f"{self.text[:57]}..."
        return preview

    def clean(self) -> None:
        super().clean()
        self._validate_options()
        self._validate_answer_key()
        if not 1 <= self.duration_seconds <= MAX_DURATION_SECONDS:
            raise ValidationError(
                {
                    "duration_seconds": (
                        f"Duration must be between 1 and {MAX_DURATION_SECONDS} "
                        "seconds."
                    )
                }
            )

    def _validate_options(self) -> None:
        """Reject option sets that cannot be rendered or answered sensibly."""
        options = self.options or []

        if self.question_type == QuestionType.TRUE_FALSE:
            if options:
                raise ValidationError(
                    {"options": "A true/false question must not define options."}
                )
            return

        if self.question_type in CONFIGURED_TYPES:
            # These types describe themselves in `type_config`. A leftover
            # `options` list on one of them would be invisible data that no
            # screen would ever render, so it is refused rather than ignored.
            if options:
                raise ValidationError(
                    {
                        "options": (
                            f"A {self.get_question_type_display().lower()} question "
                            "does not use options; describe it in type_config."
                        )
                    }
                )
            return

        if not 2 <= len(options) <= 6:
            raise ValidationError(
                {
                    "options": (
                        "A multiple choice question needs between 2 and 6 options; "
                        f"got {len(options)}."
                    )
                }
            )

        if any(not str(option).strip() for option in options):
            raise ValidationError({"options": "Options must not be blank."})

    def _validate_answer_key(self) -> None:
        """Require exactly one answer key, and one that matches the type.

        A question with no key cannot be scored, and a question with two is
        ambiguous about what "correct" even means. Neither is allowed to reach a
        live round: it would either strand every classroom with no points, or
        silently award them on a coin toss.
        """
        errors: dict[str, str] = {}

        if self.question_type in CONFIGURED_TYPES:
            errors.update(self._configured_key_errors())
        elif self.question_type == QuestionType.TRUE_FALSE:
            if self.correct_answer is None:
                errors["correct_answer"] = "A true/false question needs an answer key."
            if self.correct_option:
                errors["correct_option"] = (
                    "Use correct_answer for a true/false question, not an option."
                )
        else:
            if not self.correct_option:
                errors["correct_option"] = (
                    "A multiple choice question needs one correct option."
                )
            elif self.correct_option not in (self.options or []):
                errors["correct_option"] = (
                    f"{self.correct_option!r} is not one of the options."
                )
            if self.correct_answer is not None:
                errors["correct_answer"] = (
                    "Use correct_option for a multiple choice question."
                )

        if errors:
            raise ValidationError(errors)

    def _configured_key_errors(self) -> dict[str, str]:
        """Validate the key of a type described by ``type_config``.

        Every failure is reported against ``type_config`` so the teacher is sent
        to the one field that describes the question, rather than being told about
        a column the form is not showing them.
        """
        config = self.type_config or {}
        errors: dict[str, str] = {}

        if self.correct_option:
            errors["type_config"] = (
                "This question type has no options, so clear correct_option."
            )
        if self.correct_answer is not None:
            errors["type_config"] = (
                "This question type has no true/false key, so clear correct_answer."
            )

        if self.question_type == QuestionType.ORDERING:
            errors.update(self._ordering_key_errors(config))
        elif self.question_type == QuestionType.CLASSIFICATION:
            errors.update(self._classification_key_errors(config))
        elif self.question_type == QuestionType.SHORT_ANSWER:
            errors.update(self._short_answer_key_errors(config))

        return errors

    @staticmethod
    def _ordering_key_errors(config: dict) -> dict[str, str]:
        items = config.get("items") or []
        order = config.get("correct_order") or []
        errors: dict[str, str] = {}

        if not isinstance(items, list) or not all(
            isinstance(item, str) for item in items
        ):
            return {"type_config": "'items' must be a list of text."}

        if not MIN_ORDERING_ITEMS <= len(items) <= MAX_ORDERING_ITEMS:
            errors["type_config"] = (
                f"An ordering question needs between {MIN_ORDERING_ITEMS} and "
                f"{MAX_ORDERING_ITEMS} items; got {len(items)}."
            )
        elif len({collapse_whitespace(item) for item in items}) != len(items):
            errors["type_config"] = "Ordering items must all be different."

        if not isinstance(order, list) or not all(
            isinstance(item, str) for item in order
        ):
            errors["type_config"] = "'correct_order' must be a list of text."
        elif not order:
            errors["type_config"] = "An ordering question needs a correct order."
        elif sorted(collapse_whitespace(item) for item in order) != sorted(
            collapse_whitespace(item) for item in items
        ):
            # The single most likely authoring mistake, so it gets its own
            # message: an order that is not a permutation of the items could
            # never be produced by a screen.
            errors["type_config"] = (
                "The correct order must contain exactly the items given, once each."
            )

        return errors

    @staticmethod
    def _classification_key_errors(config: dict) -> dict[str, str]:
        categories = config.get("categories") or []
        assignments = config.get("assignments") or []
        errors: dict[str, str] = {}

        if not isinstance(categories, list) or not all(
            isinstance(name, str) for name in categories
        ):
            return {"type_config": "'categories' must be a list of category names."}

        if not MIN_CATEGORIES <= len(categories) <= MAX_CATEGORIES:
            errors["type_config"] = (
                f"A classification question needs between {MIN_CATEGORIES} and "
                f"{MAX_CATEGORIES} categories; got {len(categories)}."
            )
        elif len({collapse_whitespace(name) for name in categories}) != len(categories):
            errors["type_config"] = "Categories must all be different."

        if not isinstance(assignments, list):
            return {
                **errors,
                "type_config": "'assignments' must be a list of items to place.",
            }

        if not MIN_CLASSIFICATION_ITEMS <= len(assignments) <= MAX_CLASSIFICATION_ITEMS:
            errors["type_config"] = (
                f"A classification question needs between {MIN_CLASSIFICATION_ITEMS} "
                f"and {MAX_CLASSIFICATION_ITEMS} items; got {len(assignments)}."
            )

        known = {collapse_whitespace(name) for name in categories}
        seen: set[str] = set()
        for assignment in assignments:
            if not isinstance(assignment, dict):
                errors["type_config"] = (
                    "Each item to place needs a 'label' and a 'category'."
                )
                break
            label = assignment.get("label")
            category = assignment.get("category")
            if not isinstance(label, str) or is_blank(label):
                errors["type_config"] = "Each item to place needs a label."
                break
            if not isinstance(category, str) or is_blank(category):
                errors["type_config"] = f"'{collapse_whitespace(label)}' needs a category."
                break
            if collapse_whitespace(category) not in known:
                errors["type_config"] = (
                    f"{collapse_whitespace(category)!r} is not one of the categories."
                )
                break
            if collapse_whitespace(label) in seen:
                errors["type_config"] = (
                    f"{collapse_whitespace(label)!r} is listed more than once."
                )
                break
            seen.add(collapse_whitespace(label))

        return errors

    @staticmethod
    def _short_answer_key_errors(config: dict) -> dict[str, str]:
        answers = config.get("accepted_answers") or []
        errors: dict[str, str] = {}

        if not isinstance(answers, list) or not all(
            isinstance(answer, str) for answer in answers
        ):
            return {"type_config": "'accepted_answers' must be a list of text."}

        if not MIN_ACCEPTED_ANSWERS <= len(answers) <= MAX_ACCEPTED_ANSWERS:
            errors["type_config"] = (
                f"A short answer question needs between {MIN_ACCEPTED_ANSWERS} and "
                f"{MAX_ACCEPTED_ANSWERS} accepted answers; got {len(answers)}."
            )

        if any(is_blank(answer) for answer in answers):
            errors["type_config"] = "Accepted answers must not be blank."
        elif any(len(answer) > MAX_ACCEPTED_ANSWER_LENGTH for answer in answers):
            errors["type_config"] = (
                f"Accepted answers may be at most {MAX_ACCEPTED_ANSWER_LENGTH} "
                "characters."
            )

        if not isinstance(config.get("case_sensitive", False), bool):
            errors["type_config"] = "'case_sensitive' must be true or false."

        return errors

    # -- answer key --------------------------------------------------------

    @property
    def config(self) -> dict:
        """The type configuration, always a dict.

        Tolerates a null or non-dict value stored by an earlier version rather
        than raising during a live round, so one bad row degrades to "no key"
        instead of taking a screen's consumer down.
        """
        value = self.type_config
        return value if isinstance(value, dict) else {}

    @property
    def ordering_items(self) -> list[str]:
        return [str(item) for item in (self.config.get("items") or [])]

    @property
    def categories(self) -> list[str]:
        return [str(name) for name in (self.config.get("categories") or [])]

    @property
    def assignments(self) -> list[dict]:
        """The key: one ``{label, category}`` per item, in display order."""
        rows = self.config.get("assignments") or []
        return [row for row in rows if isinstance(row, dict)]

    @property
    def accepted_answers(self) -> list[str]:
        return [str(answer) for answer in (self.config.get("accepted_answers") or [])]

    @property
    def case_sensitive(self) -> bool:
        return bool(self.config.get("case_sensitive", False))

    @property
    def has_answer_key(self) -> bool:
        """Whether this question can be scored.

        Used to keep an unscorable question out of a competition, so the teacher
        finds out while setting the round up rather than mid-lesson.
        """
        if self.question_type in CONFIGURED_TYPES:
            # Deliberately reuses the same validation the form uses, rather than a
            # second, looser set of rules: a key the admin would refuse must not
            # be treated as scoreable by the live engine.
            return not self._configured_key_errors()
        if self.question_type == QuestionType.TRUE_FALSE:
            return self.correct_answer is not None
        return bool(self.correct_option) and self.correct_option in (self.options or [])

    @property
    def answer_key(self) -> str | None:
        """The correct answer as a single comparable string.

        Every type is reduced to one string so that storage and reporting do not
        have to branch on type, and so ``Answer.correct_answer`` can hold the key
        that was in force when an answer was scored. Comparison itself does *not*
        go through this string - :meth:`matches` compares the real structures,
        because "1, 2, 3" and "3, 2, 1" are different orders and a string
        comparison of a joined list would be the wrong shape of test.

        Returns ``None`` when the key is unset, which callers must treat as
        "cannot be scored" rather than "never correct".
        """
        if not self.has_answer_key:
            return None

        if self.question_type == QuestionType.TRUE_FALSE:
            return "true" if self.correct_answer else "false"
        if self.question_type == QuestionType.ORDERING:
            return " > ".join(self.config.get("correct_order") or [])
        if self.question_type == QuestionType.CLASSIFICATION:
            return ", ".join(
                f"{row.get('label')} = {row.get('category')}"
                for row in self.assignments
            )
        if self.question_type == QuestionType.SHORT_ANSWER:
            # The first listed answer is the one shown on the reveal, so it is
            # the teacher's preferred wording of a correct answer.
            return collapse_whitespace(self.accepted_answers[0])
        return self.correct_option

    def accepts(self, candidate) -> bool:
        """Whether ``candidate`` is an answer a screen could legitimately give.

        Structural, not semantic: it checks the shape of the selection against the
        shape of the question. Whether the answer is *right* is
        :meth:`matches`, and the two are deliberately separate so that a
        malformed submission is refused with a different message from a wrong one.
        """
        if self.question_type == QuestionType.ORDERING:
            return self._accepts_ordering(candidate)
        if self.question_type == QuestionType.CLASSIFICATION:
            return self._accepts_classification(candidate)
        if self.question_type == QuestionType.SHORT_ANSWER:
            # Any text is structurally valid for a short answer; only the
            # teacher's key decides whether it is right.
            return isinstance(candidate, str) and not is_blank(candidate)
        if self.question_type == QuestionType.TRUE_FALSE:
            return str(candidate).strip().lower() in {"true", "false"}
        return candidate in (self.options or [])

    def _accepts_ordering(self, candidate) -> bool:
        if not isinstance(candidate, list):
            return False
        if not all(isinstance(item, str) for item in candidate):
            return False
        proposed = [collapse_whitespace(item) for item in candidate]
        expected = [collapse_whitespace(item) for item in self.ordering_items]
        # Every item exactly once: duplicates would let a screen pad its answer
        # to look longer than it is, and a missing item means it never finished.
        return sorted(proposed) == sorted(expected)

    def _accepts_classification(self, candidate) -> bool:
        placements = self._coerce_assignments(candidate)
        if placements is None:
            return False
        expected = {
            collapse_whitespace(row.get("label", "")): row.get("category")
            for row in self.assignments
        }
        if set(placements) != set(expected):
            return False
        categories = {collapse_whitespace(name) for name in self.categories}
        return all(
            collapse_whitespace(category) in categories
            for category in placements.values()
        )

    @staticmethod
    def _coerce_assignments(candidate) -> dict[str, str] | None:
        """Accept either ``[{"label", "category"}]`` or ``{"label": "category"}``.

        A list preserves the order the teacher authored and the order a screen
        sent; a mapping is more natural for a simple client. Both are accepted so
        the screen page and a test script can each use the simpler one, and both
        are reduced to the same mapping before anything is compared.
        """
        if isinstance(candidate, dict):
            pairs = candidate.items()
        elif isinstance(candidate, list):
            pairs = []
            for row in candidate:
                if not isinstance(row, dict):
                    return None
                label = row.get("label", row.get("item"))
                if label is None:
                    return None
                pairs.append((label, row.get("category")))
        else:
            return None

        placements: dict[str, str] = {}
        for label, category in pairs:
            if not isinstance(label, str) or not isinstance(category, str):
                return None
            key = collapse_whitespace(label)
            if not key or key in placements:
                # A repeated item is malformed, not merely wrong: the screen has
                # two placements for one thing and no way to say which it meant.
                return None
            placements[key] = category
        return placements

    def matches(self, candidate) -> bool:
        """Whether ``candidate`` is the correct answer to this question.

        This is the only thing that decides correctness, and it runs entirely on
        the server against the stored key.
        """
        if not self.accepts(candidate):
            return False

        if self.question_type == QuestionType.ORDERING:
            proposed = [collapse_whitespace(item) for item in candidate]
            key = [collapse_whitespace(item) for item in self.config.get("correct_order") or []]
            return proposed == key

        if self.question_type == QuestionType.CLASSIFICATION:
            proposed = self._coerce_assignments(candidate)
            key = {
                collapse_whitespace(row.get("label", "")): row.get("category")
                for row in self.assignments
            }
            return all(
                collapse_whitespace(proposed[label]) == collapse_whitespace(category)
                for label, category in key.items()
            )

        if self.question_type == QuestionType.SHORT_ANSWER:
            given = normalise_short_answer(candidate, case_sensitive=self.case_sensitive)
            return any(
                given
                == normalise_short_answer(answer, case_sensitive=self.case_sensitive)
                for answer in self.accepted_answers
            )

        if self.question_type == QuestionType.TRUE_FALSE:
            # Compared case-insensitively, because `accepts` above is: a screen
            # that renders the buttons as "True" and "False" sends that casing,
            # while the key is stored lowercase. Without this the two methods
            # would disagree - an answer accepted as well-formed would be scored
            # wrong, which is the worst possible outcome for a pupil.
            key = self.answer_key
            return key is not None and normalise_short_answer(
                candidate
            ) == normalise_short_answer(key)

        key = self.answer_key
        return key is not None and candidate == key

    def describe_selection(self, selection) -> str:
        """A selection rendered as one line, for reports and the reveal.

        Mirrors :attr:`Answer.selection`, so an answer reads the same whether it
        is being shown on a screen, in an admin, or in a report.
        """
        if self.question_type == QuestionType.ORDERING and isinstance(selection, list):
            return " → ".join(str(item) for item in selection)
        if self.question_type == QuestionType.CLASSIFICATION:
            placements = self._coerce_assignments(selection) or {}
            return ", ".join(
                f"{label} = {category}" for label, category in placements.items()
            )
        if isinstance(selection, str):
            return selection
        return str(selection)

    # -- payload for the live engine ---------------------------------------

    def as_live_payload(self, number: int, total: int, duration_seconds: int) -> dict:
        """Build the question portion of a ``question_started`` event.

        Deliberately returns no answer key and no explanation: this is exactly
        what a screen needs to render the question and choose from it. Both stay
        on the server until the answer period ends, so they cannot leak to a
        screen even by accident - see ``scoring.services.question_breakdown``.

        The ``presentation`` key carries whatever the type needs to draw itself,
        which is what lets a new question type be added without touching the
        screen's JavaScript dispatch.
        """
        payload = {
            "question_id": self.pk,
            "text": self.text,
            "question_type": self.question_type,
            "options": list(self.options or []),
            "image_url": self.image.url if self.image else None,
            "duration_seconds": duration_seconds,
            "question_number": number,
            "total_questions": total,
            "presentation": self.presentation_payload(),
        }
        return payload

    def presentation_payload(self) -> dict:
        """What a screen needs to draw this question, and nothing more.

        Each type gets the keys its own control needs, and no key from the other
        types is present - so a screen cannot accidentally render, and a curious
        browser cannot read, another type's data.
        """
        if self.question_type == QuestionType.ORDERING:
            return {
                "items": self.ordering_items,
                "min_items": len(self.ordering_items),
            }
        if self.question_type == QuestionType.CLASSIFICATION:
            return {
                "categories": self.categories,
                "items": [row.get("label") for row in self.assignments],
            }
        if self.question_type == QuestionType.SHORT_ANSWER:
            return {
                "max_length": max(
                    (len(answer) for answer in self.accepted_answers), default=0
                ),
                "multiline": bool(self.config.get("multiline", False)),
            }
        return {}
