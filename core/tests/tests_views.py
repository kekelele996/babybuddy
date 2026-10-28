# -*- coding: utf-8 -*-
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import TestCase
from django.test import Client as HttpClient
from django.utils import timezone

from faker import Faker

from core import models


class ViewsTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super(ViewsTestCase, cls).setUpClass()
        fake = Faker()
        call_command("migrate", verbosity=0)
        call_command("fake", verbosity=0)

        cls.c = HttpClient()

        fake_user = fake.simple_profile()
        cls.credentials = {
            "username": fake_user["username"],
            "password": fake.password(),
        }
        cls.user = get_user_model().objects.create_user(
            is_superuser=True, **cls.credentials
        )

        cls.c.login(**cls.credentials)

    def test_bmi_views(self):
        page = self.c.get("/bmi/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/bmi/add/")
        self.assertEqual(page.status_code, 200)

        entry = models.BMI.objects.first()
        page = self.c.get("/bmi/{}/".format(entry.id))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/bmi/{}/delete/".format(entry.id))
        self.assertEqual(page.status_code, 200)

    def test_child_views(self):
        page = self.c.get("/children/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/children/add/")
        self.assertEqual(page.status_code, 200)

        entry = models.Child.objects.first()
        page = self.c.get("/children/{}/".format(entry.slug))
        self.assertEqual(page.status_code, 200)
        page = self.c.get(
            "/children/{}/".format(entry.slug),
            {"date": timezone.localdate() - timezone.timedelta(days=1)},
        )
        self.assertEqual(page.status_code, 200)

        page = self.c.get("/children/{}/edit/".format(entry.slug))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/children/{}/delete/".format(entry.slug))
        self.assertEqual(page.status_code, 200)

    def test_diaperchange_views(self):
        page = self.c.get("/changes/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/changes/add/")
        self.assertEqual(page.status_code, 200)

        entry = models.DiaperChange.objects.first()
        page = self.c.get("/changes/{}/".format(entry.id))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/changes/{}/delete/".format(entry.id))
        self.assertEqual(page.status_code, 200)

    def test_feeding_views(self):
        page = self.c.get("/feedings/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/feedings/add/")
        self.assertEqual(page.status_code, 200)

        entry = models.Feeding.objects.first()
        page = self.c.get("/feedings/{}/".format(entry.id))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/feedings/{}/delete/".format(entry.id))
        self.assertEqual(page.status_code, 200)

    def test_headcircumference_views(self):
        page = self.c.get("/head-circumference/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/head-circumference/add/")
        self.assertEqual(page.status_code, 200)

        entry = models.HeadCircumference.objects.first()
        page = self.c.get("/head-circumference/{}/".format(entry.id))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/head-circumference/{}/delete/".format(entry.id))
        self.assertEqual(page.status_code, 200)

    def test_height_views(self):
        page = self.c.get("/height/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/height/add/")
        self.assertEqual(page.status_code, 200)

        entry = models.Height.objects.first()
        page = self.c.get("/height/{}/".format(entry.id))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/height/{}/delete/".format(entry.id))
        self.assertEqual(page.status_code, 200)

    def test_note_views(self):
        page = self.c.get("/notes/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/notes/add/")
        self.assertEqual(page.status_code, 200)

        entry = models.Note.objects.first()
        page = self.c.get("/notes/{}/".format(entry.id))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/notes/{}/delete/".format(entry.id))
        self.assertEqual(page.status_code, 200)

    def test_pumping_views(self):
        page = self.c.get("/pumping/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/pumping/add/")
        self.assertEqual(page.status_code, 200)

        entry = models.Pumping.objects.first()
        page = self.c.get("/pumping/{}/".format(entry.id))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/pumping/{}/delete/".format(entry.id))
        self.assertEqual(page.status_code, 200)

    def test_sleep_views(self):
        page = self.c.get("/sleep/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/sleep/add/")
        self.assertEqual(page.status_code, 200)

        entry = models.Sleep.objects.first()
        page = self.c.get("/sleep/{}/".format(entry.id))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/sleep/{}/delete/".format(entry.id))
        self.assertEqual(page.status_code, 200)

    def test_tags_views(self):
        page = self.c.get("/tags/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/tags/add/")
        self.assertEqual(page.status_code, 200)

        entry = models.Tag.objects.first()
        page = self.c.get("/tags/{}/".format(entry.slug))
        self.assertEqual(page.status_code, 200)
        entry = models.Tag.objects.first()
        page = self.c.get("/tags/{}/edit".format(entry.slug))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/tags/{}/delete/".format(entry.slug))
        self.assertEqual(page.status_code, 200)

    def test_temperature_views(self):
        page = self.c.get("/temperature/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/temperature/add/")
        self.assertEqual(page.status_code, 200)

        entry = models.Temperature.objects.first()
        page = self.c.get("/temperature/{}/".format(entry.id))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/temperature/{}/delete/".format(entry.id))
        self.assertEqual(page.status_code, 200)

    def test_medication_views(self):
        page = self.c.get("/medication/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/medication/add/")
        self.assertEqual(page.status_code, 200)

        entry = models.Medication.objects.first()
        page = self.c.get("/medication/{}/".format(entry.id))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/medication/{}/delete/".format(entry.id))
        self.assertEqual(page.status_code, 200)

    def test_timer_views(self):
        page = self.c.get("/timers/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/timers/add/")
        self.assertEqual(page.status_code, 200)

        page = self.c.get("/timers/add/quick/")
        self.assertEqual(page.status_code, 405)
        page = self.c.post("/timers/add/quick/", follow=True)
        self.assertEqual(page.status_code, 200)

        entry = models.Timer.objects.first()
        page = self.c.get("/timers/{}/".format(entry.id))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/timers/{}/edit/".format(entry.id))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/timers/{}/delete/".format(entry.id))
        self.assertEqual(page.status_code, 200)

        page = self.c.get("/timers/{}/restart/".format(entry.id))
        self.assertEqual(page.status_code, 405)
        page = self.c.post("/timers/{}/restart/".format(entry.id), follow=True)
        self.assertEqual(page.status_code, 200)

    def test_timer_add_quick_assigns_posted_child(self):
        child = models.Child.objects.first()
        models.Child.objects.create(
            first_name="Second", last_name="Child", birth_date="2000-01-01"
        )
        response = self.c.post("/timers/add/quick/", {"child": child.pk}, follow=True)
        self.assertEqual(response.status_code, 200)
        timer = models.Timer.objects.latest("id")
        self.assertEqual(timer.child, child)

    def test_quick_timer_buttons_post_child_as_hidden_input(self):
        models.Child.objects.create(
            first_name="Second", last_name="Child", birth_date="2000-01-01"
        )
        page = self.c.get("/timers/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'type="hidden" name="child"')
        self.assertNotRegex(page.content.decode("utf-8"), r"<button[^>]*name=\"child\"")

    def test_compact_quick_timer_buttons_post_child_as_hidden_input(self):
        models.Child.objects.create(
            first_name="Second", last_name="Child", birth_date="2000-01-01"
        )
        child = models.Child.objects.first()
        page = self.c.get("/children/{}/".format(child.slug))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="quick-timer-menu-toggle"')
        self.assertContains(page, 'type="hidden" name="child"')
        self.assertNotRegex(page.content.decode("utf-8"), r"<button[^>]*name=\"child\"")

    def test_timeline_views(self):
        child = models.Child.objects.first()
        response = self.c.get("/timeline/")
        self.assertRedirects(response, "/children/{}/".format(child.slug))

        models.Child.objects.create(
            first_name="Second", last_name="Child", birth_date="2000-01-01"
        )
        response = self.c.get("/timeline/")
        self.assertEqual(response.status_code, 200)

    def test_tummytime_views(self):
        page = self.c.get("/tummy-time/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/tummy-time/add/")
        self.assertEqual(page.status_code, 200)

        entry = models.TummyTime.objects.first()
        page = self.c.get("/tummy-time/{}/".format(entry.id))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/tummy-time/{}/delete/".format(entry.id))
        self.assertEqual(page.status_code, 200)

    def test_weight_views(self):
        page = self.c.get("/weight/")
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/weight/add/")
        self.assertEqual(page.status_code, 200)

        entry = models.Weight.objects.first()
        page = self.c.get("/weight/{}/".format(entry.id))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/weight/{}/delete/".format(entry.id))
        self.assertEqual(page.status_code, 200)


CARE_ENTRY_PERMISSIONS = (
    "view_child",
    "view_timer",
    "view_feeding",
    "view_diaperchange",
    "view_sleep",
)


class TimelinePermissionsTestCase(TestCase):
    """
    The timeline pulls events from every core model. It only includes the
    event types the user has the matching `view` permission for.
    """

    fixtures = ["tests.json"]

    def setUp(self):
        self.child = models.Child.objects.first()
        now = timezone.localtime()
        models.Medication.objects.create(
            child=self.child, name="Timeline Medication", time=now
        )
        models.Note.objects.create(
            child=self.child, note="Timeline private note", time=now
        )
        models.Temperature.objects.create(child=self.child, temperature=38.9, time=now)
        models.TummyTime.objects.create(
            child=self.child, start=now, end=now + timezone.timedelta(minutes=5)
        )
        models.Feeding.objects.create(
            child=self.child,
            start=now,
            end=now + timezone.timedelta(minutes=10),
            type="formula",
            method="bottle",
            amount=100,
        )
        self.c = HttpClient()
        self.url = "/children/{}/".format(self.child.slug)

    def _login(self, username, codenames=None, read_only=False):
        user = get_user_model().objects.create_user(
            username=username, password="password", is_active=True
        )
        if read_only:
            # The `read_only` group holds `view` on every core model. The
            # permissions are granted to the user directly, rather than by
            # looking the group up, so this keeps working whether or not the
            # group has been populated.
            codenames = tuple(
                Permission.objects.filter(
                    content_type__app_label="core", codename__startswith="view_"
                ).values_list("codename", flat=True)
            )
        if codenames:
            user.user_permissions.add(
                *Permission.objects.filter(
                    content_type__app_label="core", codename__in=codenames
                )
            )
        self.c.login(username=username, password="password")
        return user

    def _model_names(self, page):
        return {event.get("model_name") for event in page.context["timeline_objects"]}

    def test_timeline_excludes_models_without_permission(self):
        self._login("carer", codenames=CARE_ENTRY_PERMISSIONS)
        page = self.c.get(self.url)
        self.assertEqual(page.status_code, 200)

        model_names = self._model_names(page)
        self.assertIn("feeding", model_names)
        for excluded in ["medication", "note", "temperature", "tummytime"]:
            self.assertNotIn(excluded, model_names)

        content = page.content.decode()
        self.assertNotIn("Timeline Medication", content)
        self.assertNotIn("Timeline private note", content)

    def test_read_only_user_sees_the_whole_timeline(self):
        self._login("readonly", read_only=True)
        page = self.c.get(self.url)
        self.assertEqual(page.status_code, 200)

        model_names = self._model_names(page)
        self.assertIn("feeding", model_names)
        self.assertIn("medication", model_names)
        self.assertIn("note", model_names)

        content = page.content.decode()
        self.assertIn("Timeline Medication", content)
        self.assertIn("Timeline private note", content)


class ChildArchiveViewsTestCase(TestCase):
    """
    Archiving hides a child from default selections (list, add forms, quick
    timer) but never deletes data: direct timeline/report access keeps
    working and the child can be restored.
    """

    def setUp(self):
        call_command("migrate", verbosity=0)
        # migrate commits inside the test transaction, so every fixture has
        # to be unique across the test methods in this class. The admin user
        # is seeded by the custom migrate command.
        self.suffix = self.id().split(".")[-1]
        self.c = HttpClient()
        self.c.login(username="admin", password="admin")

        self.active = models.Child.objects.create(
            first_name="Active {}".format(self.suffix),
            last_name="Child",
            birth_date="2020-01-01",
        )
        self.archived = models.Child.objects.create(
            first_name="Archived {}".format(self.suffix),
            last_name="Child",
            birth_date="2018-01-01",
        )
        now = timezone.localtime()
        self.feeding = models.Feeding.objects.create(
            child=self.archived,
            start=now - timezone.timedelta(hours=1),
            end=now - timezone.timedelta(minutes=55),
            type="formula",
            method="bottle",
        )
        self.archived.archive()

    def test_list_hides_archived_by_default(self):
        page = self.c.get("/children/")
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.context["show_archived"])
        self.assertGreaterEqual(page.context["archived_count"], 1)
        content = page.content.decode()
        self.assertIn(self.active.first_name, content)
        self.assertNotIn(self.archived.first_name, content)
        # The toggle keeps a single, well-formed query string.
        self.assertIn('href="?archived=1"', content)
        self.assertNotIn("??archived", content)

    def test_list_shows_archived_on_demand(self):
        page = self.c.get("/children/", {"archived": "1"})
        self.assertTrue(page.context["show_archived"])
        content = page.content.decode()
        self.assertIn(self.active.first_name, content)
        self.assertIn(self.archived.first_name, content)
        self.assertIn("/children/{}/restore/".format(self.archived.slug), content)

    def test_archive_confirmation_page(self):
        page = self.c.get("/children/{}/archive/".format(self.active.slug))
        self.assertEqual(page.status_code, 200)
        content = page.content.decode()
        self.assertIn("Archive Child", content)
        # The confirmation explicitly states that nothing is deleted.
        self.assertIn("No records", content)

    def test_archive_action(self):
        response = self.c.post("/children/{}/archive/".format(self.active.slug))
        self.assertRedirects(response, "/children/")
        self.active.refresh_from_db()
        self.assertTrue(self.active.archived)
        # The child and its data are still in the database.
        self.assertTrue(models.Child.objects.filter(pk=self.active.pk).exists())
        self.assertFalse(models.Child.active.filter(pk=self.active.pk).exists())
        self.assertTrue(models.Feeding.objects.filter(pk=self.feeding.pk).exists())

    def test_restore_action(self):
        child_url = "/children/{}/".format(self.archived.slug)
        active_before = models.Child.active.count()
        response = self.c.post(
            "/children/{}/restore/".format(self.archived.slug),
            {"next": child_url},
        )
        self.assertRedirects(response, child_url)
        self.archived.refresh_from_db()
        self.assertFalse(self.archived.archived)
        self.assertEqual(models.Child.active.count(), active_before + 1)

    def test_restore_rejects_unsafe_redirect(self):
        response = self.c.post(
            "/children/{}/restore/".format(self.archived.slug),
            {"next": "https://example.com/elsewhere"},
        )
        self.assertRedirects(response, "/children/")

    def test_restore_requires_post(self):
        page = self.c.get("/children/{}/restore/".format(self.archived.slug))
        self.assertEqual(page.status_code, 405)

    def test_archived_child_pages_remain_readable(self):
        page = self.c.get("/children/{}/".format(self.archived.slug))
        self.assertEqual(page.status_code, 200)
        self.assertIn("Archived", page.content.decode())
        page = self.c.get("/children/{}/dashboard/".format(self.archived.slug))
        self.assertEqual(page.status_code, 200)
        page = self.c.get("/children/{}/reports".format(self.archived.slug))
        self.assertEqual(page.status_code, 200)
        # The feeding history is still there.
        self.assertTrue(models.Feeding.objects.filter(child=self.archived).exists())

    def test_add_form_hides_archived_child_choices(self):
        page = self.c.get("/feedings/add/")
        self.assertEqual(page.status_code, 200)
        choices = page.context["form"].fields["child"].queryset
        self.assertIn(self.active, choices)
        self.assertNotIn(self.archived, choices)

    def test_prefilled_add_form_keeps_archived_child_choice(self):
        page = self.c.get("/feedings/add/", {"child": self.archived.slug})
        self.assertEqual(page.context["form"].initial["child"].pk, self.archived.pk)
        choices = page.context["form"].fields["child"].queryset
        self.assertIn(self.archived, choices)

    def test_edit_form_keeps_the_related_archived_child(self):
        page = self.c.get("/feedings/{}/".format(self.feeding.id))
        self.assertEqual(page.status_code, 200)
        choices = page.context["form"].fields["child"].queryset
        self.assertIn(self.archived, choices)
        self.assertIn(self.active, choices)

    def test_quick_timer_nav_uses_active_children_only(self):
        page = self.c.get("/children/")
        content = page.content.decode()
        # One active child means the single quick-timer button, not a list
        # containing the archived child.
        self.assertNotIn('name="child" value="{}"'.format(self.archived.pk), content)

    def test_quick_timer_auto_assigns_the_active_child(self):
        self.c.post("/timers/add/quick/")
        timer = models.Timer.objects.filter(user__username="admin").latest("id")
        self.assertEqual(timer.child, self.active)

    def test_timeline_redirects_to_single_active_child(self):
        response = self.c.get("/timeline/")
        self.assertRedirects(response, "/children/{}/".format(self.active.slug))

    def test_archive_requires_change_permission(self):
        self.c.logout()
        username = "readonly-{}".format(self.suffix)
        get_user_model().objects.create_user(username=username, password="readonly")
        self.c.login(username=username, password="readonly")
        page = self.c.post("/children/{}/archive/".format(self.active.slug))
        self.assertEqual(page.status_code, 403)
        self.active.refresh_from_db()
        self.assertFalse(self.active.archived)
