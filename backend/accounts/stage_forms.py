from django import forms
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm
from django.utils.translation import gettext_lazy as _
from accounts.models import User
from classrooms.models import SchoolStage


class DeputyScopeFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['deputy_stages'] = forms.ModelMultipleChoiceField(
            label=_("Assigned stages"), queryset=SchoolStage.objects.all(), required=False,
            help_text=_("Select one or more stages. No assignment grants no monitoring access."),
        )
        if self.instance.pk:
            self.initial['deputy_stages'] = self.instance.deputy_stages.values_list('pk', flat=True)

    def clean(self):
        cleaned = super().clean()
        stages = cleaned.get('deputy_stages')
        if cleaned.get('role') != 'stage_deputy' and stages:
            self.add_error('deputy_stages', _("Only a Stage Deputy may be assigned stages."))
        if cleaned.get('role') == 'stage_deputy' and cleaned.get('is_superuser'):
            self.add_error('is_superuser', _("A Stage Deputy cannot be a superuser."))
        return cleaned

    def _save_m2m(self):
        super()._save_m2m()
        self.instance.deputy_stages.set(self.cleaned_data.get('deputy_stages') or [])


class DeputyUserChangeForm(DeputyScopeFormMixin, UserChangeForm):
    deputy_stages = forms.ModelMultipleChoiceField(queryset=SchoolStage.objects.all(), required=False)
    class Meta(UserChangeForm.Meta):
        model = User
        fields = '__all__'


class DeputyUserCreationForm(DeputyScopeFormMixin, AdminUserCreationForm):
    deputy_stages = forms.ModelMultipleChoiceField(queryset=SchoolStage.objects.all(), required=False)
    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ('username', 'full_name', 'email', 'role', 'deputy_stages')
