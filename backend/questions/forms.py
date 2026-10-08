"""Teacher-facing form for the Question Builder.

Reuses the existing :class:`questions.models.Question` model without changing
it. The JSON fields ``options``, ``correct_option`` and ``type_config`` are
flattened into plain form fields so the template can render type-specific widgets
without branching on model internals.
"""

from __future__ import annotations

import json

from django import forms
from django.core.exceptions import ValidationError

from questions.models import Question, QuestionType
from questions.normalisation import collapse_whitespace, is_blank, normalise_short_answer


QUESTION_TYPE_CHOICES = list(QuestionType.choices)


class TeacherQuestionForm(forms.Form):
    """A single form that handles every question type the model supports."""

    text = forms.CharField(
        label="Question text",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Write your question…"}),
    )
    question_type = forms.ChoiceField(
        choices=QUESTION_TYPE_CHOICES,
        widget=forms.RadioSelect,
        initial=QuestionType.MULTIPLE_CHOICE,
    )
    options = forms.CharField(widget=forms.HiddenInput, required=False, initial="[]")
    correct_option = forms.CharField(
        widget=forms.HiddenInput, required=False, initial=""
    )
    correct_answer = forms.ChoiceField(
        choices=[("true", "True"), ("false", "False")],
        widget=forms.RadioSelect,
        required=False,
    )
    type_config = forms.CharField(widget=forms.HiddenInput, required=False, initial="{}")
    explanation = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Optional explanation…"}),
        required=False,
    )
    duration_seconds = forms.IntegerField(
        min_value=1, max_value=600, initial=30, label="Duration (seconds)"
    )
    image = forms.FileField(required=False, label="Image")
    active = forms.BooleanField(required=False, initial=True, label="Active")

    def __init__(self, *args, **kwargs):
        self.instance = kwargs.pop("instance", None)
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            self._load_instance()

    def _load_instance(self) -> None:
        """Populate form fields from an existing Question instance."""
        q = self.instance
        self.fields["text"].initial = q.text
        self.fields["question_type"].initial = q.question_type
        self.fields["options"].initial = json.dumps(q.options or [])
        self.fields["correct_option"].initial = q.correct_option or ""
        self.fields["correct_answer"].initial = (
            "true" if q.correct_answer is True else "false"
            if q.correct_answer is False
            else ""
        )
        self.fields["type_config"].initial = json.dumps(q.type_config or {})
        self.fields["explanation"].initial = q.explanation
        self.fields["duration_seconds"].initial = q.duration_seconds
        self.fields["active"].initial = q.active

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_json(self, field_name: str) -> any:
        raw = self.cleaned_data.get(field_name, "")
        if not raw or not raw.strip():
            return {} if field_name == "type_config" else []
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValidationError(
                {field_name: f"Invalid JSON: {exc}"}
            )
        return value

    # ------------------------------------------------------------------
    # clean
    # ------------------------------------------------------------------

    def clean(self) -> dict:
        cleaned = super().clean()
        q_type = cleaned.get("question_type", QuestionType.MULTIPLE_CHOICE)

        options = self._parse_json("options")
        type_config = self._parse_json("type_config")
        correct_option = cleaned.get("correct_option", "").strip()
        correct_answer_raw = cleaned.get("correct_answer")

        # Normalise correct_answer to a proper boolean for true/false type
        correct_answer = None
        if correct_answer_raw:
            correct_answer = correct_answer_raw == "true"

        # Mirror model-level option validation
        errors: dict[str, list] = {}

        if q_type == QuestionType.TRUE_FALSE:
            if options:
                errors.setdefault("options", []).append(
                    "A true/false question must not define options."
                )
            if correct_answer is None:
                errors.setdefault("correct_answer", []).append(
                    "A true/false question needs an answer key."
                )
            if correct_option:
                errors.setdefault("correct_option", []).append(
                    "Use correct_answer for a true/false question, not an option."
                )

        elif q_type in {QuestionType.ORDERING, QuestionType.CLASSIFICATION, QuestionType.SHORT_ANSWER}:
            if options:
                errors.setdefault("options", []).append(
                    "This question type does not use options; describe it in type_config."
                )
            if correct_option:
                errors.setdefault("correct_option", []).append(
                    "This question type has no options, so clear correct_option."
                )
            if correct_answer is not None:
                errors.setdefault("type_config", []).append(
                    "This question type has no true/false key, so clear correct_answer."
                )
            # Type-specific key validation
            if q_type == QuestionType.ORDERING:
                self._validate_ordering(type_config, errors)
            elif q_type == QuestionType.CLASSIFICATION:
                self._validate_classification(type_config, errors)
            elif q_type == QuestionType.SHORT_ANSWER:
                self._validate_short_answer(type_config, errors)

        else:
            # MULTIPLE_CHOICE
            if not 2 <= len(options or []) <= 6:
                errors.setdefault("options", []).append(
                    "A multiple choice question needs between 2 and 6 options; "
                    f"got {len(options or [])}."
                )
            elif any(is_blank(opt) for opt in (options or [])):
                errors.setdefault("options", []).append(
                    "Options must not be blank."
                )
            if not correct_option:
                errors.setdefault("correct_option", []).append(
                    "A multiple choice question needs one correct option."
                )
            elif correct_option not in (options or []):
                errors.setdefault("correct_option", []).append(
                    f"{correct_option!r} is not one of the options."
                )
            if correct_answer is not None:
                errors.setdefault("correct_answer", []).append(
                    "Use correct_option for a multiple choice question."
                )

        if errors:
            raise ValidationError(errors)

        # Store parsed values back for the view to consume
        cleaned["_parsed_options"] = options
        cleaned["_parsed_type_config"] = type_config
        cleaned["_parsed_correct_answer"] = correct_answer
        return cleaned

    # ------------------------------------------------------------------
    # Per-type validators
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_ordering(config: dict, errors: dict) -> None:
        items = config.get("items") or []
        order = config.get("correct_order") or []

        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            errors.setdefault("type_config", []).append("'items' must be a list of text.")
            return

        if not (2 <= len(items) <= 10):
            errors.setdefault("type_config", []).append(
                f"An ordering question needs between 2 and 10 items; got {len(items)}."
            )
        elif len({collapse_whitespace(i) for i in items}) != len(items):
            errors.setdefault("type_config", []).append(
                "Ordering items must all be different."
            )

        if not isinstance(order, list) or not all(isinstance(i, str) for i in order):
            errors.setdefault("type_config", []).append(
                "'correct_order' must be a list of text."
            )
        elif not order:
            errors.setdefault("type_config", []).append(
                "An ordering question needs a correct order."
            )
        elif sorted(collapse_whitespace(i) for i in order) != sorted(
            collapse_whitespace(i) for i in items
        ):
            errors.setdefault("type_config", []).append(
                "The correct order must contain exactly the items given, once each."
            )

    @staticmethod
    def _validate_classification(config: dict, errors: dict) -> None:
        categories = config.get("categories") or []
        assignments = config.get("assignments") or []

        if not isinstance(categories, list) or not all(isinstance(c, str) for c in categories):
            errors.setdefault("type_config", []).append(
                "'categories' must be a list of category names."
            )
            return

        if not (2 <= len(categories) <= 6):
            errors.setdefault("type_config", []).append(
                f"A classification question needs between 2 and 6 categories; "
                f"got {len(categories)}."
            )
        elif len({collapse_whitespace(c) for c in categories}) != len(categories):
            errors.setdefault("type_config", []).append(
                "Categories must all be different."
            )

        if not isinstance(assignments, list):
            errors.setdefault("type_config", []).append(
                "'assignments' must be a list of items to place."
            )
            return

        if not (2 <= len(assignments) <= 12):
            errors.setdefault("type_config", []).append(
                f"A classification question needs between 2 and 12 items; "
                f"got {len(assignments)}."
            )
            return

        known = {collapse_whitespace(c) for c in categories}
        seen: set[str] = set()
        for assignment in assignments:
            if not isinstance(assignment, dict):
                errors.setdefault("type_config", []).append(
                    "Each item to place needs a 'label' and a 'category'."
                )
                break
            label = assignment.get("label")
            category = assignment.get("category")
            if not isinstance(label, str) or is_blank(label):
                errors.setdefault("type_config", []).append("Each item to place needs a label.")
                break
            if not isinstance(category, str) or is_blank(category):
                errors.setdefault("type_config", []).append(
                    f"'{collapse_whitespace(label)}' needs a category."
                )
                break
            if collapse_whitespace(category) not in known:
                errors.setdefault("type_config", []).append(
                    f"{collapse_whitespace(category)!r} is not one of the categories."
                )
                break
            if collapse_whitespace(label) in seen:
                errors.setdefault("type_config", []).append(
                    f"{collapse_whitespace(label)!r} is listed more than once."
                )
                break
            seen.add(collapse_whitespace(label))

    @staticmethod
    def _validate_short_answer(config: dict, errors: dict) -> None:
        answers = config.get("accepted_answers") or []

        if not isinstance(answers, list) or not all(isinstance(a, str) for a in answers):
            errors.setdefault("type_config", []).append(
                "'accepted_answers' must be a list of text."
            )
            return

        if not (1 <= len(answers) <= 10):
            errors.setdefault("type_config", []).append(
                f"A short answer question needs between 1 and 10 accepted answers; "
                f"got {len(answers)}."
            )
        elif any(is_blank(a) for a in answers):
            errors.setdefault("type_config", []).append(
                "Accepted answers must not be blank."
            )
        elif any(len(a) > 200 for a in answers):
            errors.setdefault("type_config", []).append(
                "Accepted answers may be at most 200 characters."
            )

        if not isinstance(config.get("case_sensitive", False), bool):
            errors.setdefault("type_config", []).append(
                "'case_sensitive' must be true or false."
            )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, *, teacher=None):
        """Create or update a Question from cleaned form data.

        ``teacher`` is accepted for future permission tightening but is not
        currently written to any model field.
        """
        cleaned = self.cleaned_data

        q_type = cleaned["question_type"]
        options = cleaned.get("_parsed_options") or []
        type_config = cleaned.get("_parsed_type_config") or {}
        correct_answer = cleaned.get("_parsed_correct_answer")

        defaults: dict = {
            "text": cleaned["text"],
            "question_type": q_type,
            "options": options,
            "correct_option": cleaned.get("correct_option", ""),
            "correct_answer": correct_answer,
            "type_config": type_config,
            "explanation": cleaned.get("explanation", ""),
            "duration_seconds": cleaned.get("duration_seconds", 30),
            "active": cleaned.get("active", True),
        }

        image = cleaned.get("image")
        if image:
            defaults["image"] = image

        if self.instance and self.instance.pk:
            for k, v in defaults.items():
                setattr(self.instance, k, v)
            self.instance.save()
            return self.instance

        return Question.objects.create(**defaults)
