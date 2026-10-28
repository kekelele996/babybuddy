# -*- coding: utf-8 -*-
from django.core.management.base import BaseCommand

from core import images


class Command(BaseCommand):
    help = (
        "Clean up uploaded images: retry deletions that previously failed, "
        "and (with --scan) clear references to missing files and delete files "
        "that no longer reference anything."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--scan",
            action="store_true",
            help=(
                "Also scan image fields: clear rows that point at missing "
                "files and delete orphaned files from the upload directories."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="With --scan, only report what would be changed.",
        )

    def handle(self, *args, **options):
        scan = options["scan"]
        dry_run = options["dry_run"]

        if scan:
            self._scan(dry_run)

        released, deferred, kept = images.retry_pending_deletions()
        if released:
            self.stdout.write(
                self.style.SUCCESS(f"{released} pending file(s) deleted.")
            )
        if kept:
            self.stdout.write(
                self.style.WARNING(
                    f"{kept} pending file(s) kept because they are " "referenced again."
                )
            )
        if deferred:
            self.stdout.write(
                self.style.ERROR(
                    f"{deferred} pending file(s) could not be deleted and "
                    "remain queued for a later retry."
                )
            )
        if not scan and not (released or deferred or kept):
            self.stdout.write("No pending file deletions.")

    def _scan(self, dry_run):
        dangling = images.list_dangling_references()
        orphans = images.find_orphan_files()

        if dry_run:
            for _model, field_name, pk, name in dangling:
                self.stdout.write(
                    f"Would clear missing {field_name} on {_model.__name__} "
                    f"#{pk}: {name}"
                )
            for name in orphans:
                self.stdout.write(f"Would delete orphaned file: {name}")
            self.stdout.write(
                f"Dry run: {len(dangling)} dangling reference(s), "
                f"{len(orphans)} orphaned file(s)."
            )
            return

        repaired = images.clear_dangling_references()
        if repaired:
            self.stdout.write(
                self.style.WARNING(
                    f"{repaired} reference(s) to missing image file(s) " "cleared."
                )
            )

        released, deferred, _skipped = images.delete_orphan_files()
        if released:
            self.stdout.write(
                self.style.SUCCESS(f"{released} orphaned file(s) deleted.")
            )
        if deferred:
            self.stdout.write(
                self.style.ERROR(
                    f"{deferred} orphaned file(s) could not be deleted and "
                    "were queued for a later retry."
                )
            )
        if not repaired and not orphans:
            self.stdout.write("No dangling references or orphaned files found.")
