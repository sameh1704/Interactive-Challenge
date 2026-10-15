"""Teacher-facing form for the Question Builder.

The JSON columns ``options``, ``correct_option``, ``correct_answer`` and
``type_config`` are the storage format the model and the scoring engine use. They
are deliberately **not** the teacher's vocabulary: this form flattens them into
plain controls - a text input per option, a radio per candidate answer, a text
input per orderable item, a dropdown per item to classify, a line per accepted
answer - and reassembles the JSON behind the scenes on save.

``_config_fields`` is the one place that translation happens, so a teacher never
sees the words ``options``, ``correct_order``, ``categories``, ``assignments`` or
``accepted_answers`` anywhere on a page.
"""

from __future__ import annotations

import json
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from questions.models import Question, QuestionType, validate_question_image
from subjects.models import Subject


QUESTION_TYPE_CHOICES = list(QuestionType.choices)


def _topics_for(subject_id):
    """Topics belonging to one subject, in display order."""
    from subjects.models import Topic

    try:
        pk = int(subject_id)
    except (TypeError, ValueError):
        return Topic.objects.none()
    return Topic.objects.active().filter(subject_id=pk).order_by("order", "name")


class TeacherQuestionForm(forms.Form):
    """A single form that handles every question type the model supports."""

    text = forms.CharField(
        label=_("Question text"),
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": _("Write your question…")}),
    )
    subject = forms.ModelChoiceField(
        label=_("Subject"),
        queryset=Subject.objects.active().ordered(),
        empty_label=_("Choose a subject"),
        help_text=_("The school subject this question belongs to."),
    )
    topic = forms.ModelChoiceField(
        label=_("Topic (optional)"),
        queryset=None,
        required=False,
        empty_label=_("No topic"),
        help_text=_("An optional subdivision within the subject."),
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
        choices=[("true", _("True")), ("false", _("False"))],
        widget=forms.HiddenInput,
        required=False,
    )
    type_config = forms.CharField(widget=forms.HiddenInput, required=False, initial="{}")
    hint = forms.CharField(
        label=_("Hint"),
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": _("Optional clue…")}),
        required=False,
    )
    explanation = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": _("Optional explanation…")}),
        required=False,
    )
    duration_seconds = forms.IntegerField(
        min_value=1, max_value=600, initial=30, label=_("Duration (seconds)")
    )
    image = forms.ImageField(
        required=False,
        label=_("Image"),
        validators=[validate_question_image],
    )
    active = forms.BooleanField(required=False, initial=True, label=_("Active"))

    def __init__(self, *args, **kwargs):
        self.instance = kwargs.pop("instance", None)
        super().__init__(*args, **kwargs)

        # Topics only make sense within a subject, so the choices follow whichever
        # subject is being entered. On a bound form that is the submitted value;
        # on an unbound one it is the instance's subject, so editing a question
        # shows its own topic rather than an empty list.
        chosen_subject = None
        if self.is_bound:
            chosen_subject = self.data.get(self.add_prefix("subject"))
        elif self.instance is not None and getattr(self.instance, "subject_id", None):
            chosen_subject = self.instance.subject_id

        if chosen_subject:
            self.fields["topic"].queryset = _topics_for(chosen_subject)
        else:
            # An unbound create form with no subject yet: offering every topic
            # would invite picking one from the wrong subject.
            self.fields["topic"].queryset = Subject.objects.none()

        if self.instance and self.instance.pk:
            self._load_instance()

    def _load_instance(self) -> None:
        """Populate form fields from an existing Question instance."""
        q = self.instance
        self.fields["text"].initial = q.text
        self.fields["subject"].initial = q.subject_id
        self.fields["topic"].initial = q.topic_id
        self.fields["question_type"].initial = q.question_type
        self.fields["options"].initial = json.dumps(q.options or [])
        self.fields["correct_option"].initial = q.correct_option or ""
        self.fields["correct_answer"].initial = (
            "true" if q.correct_answer is True else "false"
            if q.correct_answer is False
            else ""
        )
        self.fields["type_config"].initial = json.dumps(q.type_config or {})
        self.fields["hint"].initial = q.hint
        self.fields["explanation"].initial = q.explanation
        self.fields["duration_seconds"].initial = q.duration_seconds
        self.fields["active"].initial = q.active

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_json(self, field_name: str, expected_type: type, default: Any) -> Any:
        raw = self.cleaned_data.get(field_name, "")
        if not raw or not raw.strip():
            return default
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValidationError(_("Enter valid JSON for %(field)s.") % {"field": field_name}) from exc
        if not isinstance(value, expected_type):
            # The type name is translated on its own so the whole message reads
            # correctly in Arabic rather than mixing an untranslated "a list"
            # into a translated sentence.
            expected = _("a list") if expected_type is list else _("an object")
            raise ValidationError(_("%(field)s must be %(expected)s.") % {"field": field_name, "expected": expected})
        return value

    def clean_options(self) -> list:
        return self._parse_json("options", list, [])

    def clean_type_config(self) -> dict:
        return self._parse_json("type_config", dict, {})

    # ------------------------------------------------------------------
    # clean
    # ------------------------------------------------------------------

    def clean(self) -> dict:
        cleaned = super().clean()
        q_type = cleaned.get("question_type")
        if q_type not in QuestionType.values:
            return cleaned

        options = cleaned.get("options", [])
        type_config = cleaned.get("type_config", {})
        correct_option = cleaned.get("correct_option", "").strip()
        correct_answer_raw = cleaned.get("correct_answer")

        correct_answer = None
        if correct_answer_raw:
            correct_answer = correct_answer_raw == "true"

        candidate = Question(
            text=cleaned.get("text", ""),
            subject=cleaned.get("subject"),
            topic=cleaned.get("topic"),
            question_type=q_type,
            options=options,
            correct_option=correct_option,
            correct_answer=correct_answer,
            type_config=type_config,
            hint=cleaned.get("hint", ""),
            explanation=cleaned.get("explanation", ""),
            duration_seconds=cleaned.get("duration_seconds", 30),
            active=cleaned.get("active", True),
        )
        try:
            candidate.full_clean()
        except ValidationError as exc:
            raise ValidationError(exc.message_dict) from exc

        cleaned["_parsed_options"] = options
        cleaned["_parsed_type_config"] = type_config
        cleaned["_parsed_correct_answer"] = correct_answer
        return cleaned

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
            "subject": cleaned["subject"],
            "topic": cleaned.get("topic"),
            "question_type": q_type,
            "options": options,
            "correct_option": cleaned.get("correct_option", ""),
            "correct_answer": correct_answer,
            "type_config": type_config,
            "hint": cleaned.get("hint", ""),
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
