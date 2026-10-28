from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0039_child_due_date"),
    ]

    operations = [
        migrations.AddField(
            model_name="child",
            name="archived",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Archived children are hidden from the dashboard and new "
                    "entry forms but their records remain available and can "
                    "be restored."
                ),
                verbose_name="Archived",
            ),
        ),
    ]
