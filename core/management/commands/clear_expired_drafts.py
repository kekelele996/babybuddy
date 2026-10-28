# -*- coding: utf-8 -*-
from django.core.management.base import BaseCommand

from core.models import FormDraft


class Command(BaseCommand):
    help = "Delete entry-form drafts older than the draft lifetime (7 days)."

    def handle(self, *args, **options):
        deleted = FormDraft.objects.clear_expired()
        self.stdout.write(
            self.style.SUCCESS("Deleted {} expired draft(s).".format(deleted))
        )
