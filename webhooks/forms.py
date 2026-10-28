# -*- coding: utf-8 -*-
import datetime

from django import forms
from django.contrib.admin.widgets import AdminSplitDateTime
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from webhooks.models import MIN_SECRET_LENGTH

#: Suggested handover time: the cutoff field opens on this, and can be changed.
DEFAULT_GRACE_PERIOD = datetime.timedelta(hours=24)


def suggested_cutoff():
    return (timezone.now() + DEFAULT_GRACE_PERIOD).replace(second=0, microsecond=0)


class SecretRotationForm(forms.Form):
    """
    The admin form behind "Rotate signing key".

    The cutoff is the moment the old key stops signing: it has to be in the
    future, and it is pre-filled a day out as a suggestion rather than a rule.
    A new secret left blank is generated; one typed in has to be long enough
    to be worth signing with.
    """

    expires_at = forms.SplitDateTimeField(
        label=_("Old key stops working at"),
        help_text=_(
            "Until this moment every request is signed with both keys, so the "
            "receiving application has time to take the new one. It is signed "
            "with the new key only afterwards. Pre-filled a day out; change it "
            "to whatever was agreed."
        ),
        initial=suggested_cutoff,
        widget=AdminSplitDateTime(),
    )
    new_secret = forms.CharField(
        label=_("New secret"),
        required=False,
        min_length=MIN_SECRET_LENGTH,
        help_text=_(
            "Leave blank to generate one. It is shown once, on the next page, "
            "and nowhere afterwards."
        ),
    )

    def clean_expires_at(self):
        cutoff = self.cleaned_data["expires_at"]
        if timezone.is_naive(cutoff):
            cutoff = timezone.make_aware(cutoff, timezone.get_current_timezone())
        if cutoff <= timezone.now():
            raise forms.ValidationError(
                _("The old key has to keep working until some time in the future.")
            )
        return cutoff
