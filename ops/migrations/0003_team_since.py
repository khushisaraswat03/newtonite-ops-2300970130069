import django.utils.timezone
from django.db import migrations, models
from django.db.models import F


def existing_items_have_been_with_their_team_since_creation(apps, schema_editor):
    WorkItem = apps.get_model("ops", "WorkItem")
    WorkItem.objects.update(team_since=F("created_at"))


class Migration(migrations.Migration):
    dependencies = [
        ('ops', '0002_unassigned_label'),
    ]

    operations = [
        migrations.AddField(
            model_name='workitem',
            name='team_since',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.RunPython(existing_items_have_been_with_their_team_since_creation, migrations.RunPython.noop),
    ]
