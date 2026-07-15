from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import migrations, models
from django.db.models import Count, Q
from django.utils import timezone


STREAK_FLOOR = 1
STREAK_CAP = 3
STREAK_HISTORY_DAYS = 400
STREAK_CACHE_VERSION = 1


def _bar(done_counts):
    if not done_counts:
        return STREAK_FLOOR
    ordered = sorted(done_counts)
    return max(STREAK_FLOOR, min(STREAK_CAP, ordered[len(ordered) // 2]))


def backfill_streak_cache(apps, schema_editor):
    DailyCheckIn = apps.get_model("daily", "DailyCheckIn")
    DailyParticipant = apps.get_model("daily", "DailyParticipant")

    check_ins = list(
        DailyCheckIn.objects.annotate(
            calculated_done_count=Count(
                "answers",
                filter=Q(answers__state="done"),
            )
        )
    )
    for check_in in check_ins:
        check_in.done_count = check_in.calculated_done_count
    if check_ins:
        DailyCheckIn.objects.bulk_update(
            check_ins,
            ["done_count"],
            batch_size=500,
        )

    participants = list(DailyParticipant.objects.all())
    for participant in participants:
        try:
            participant_zone = ZoneInfo(participant.timezone) if participant.timezone else None
        except ZoneInfoNotFoundError:
            participant_zone = None
        today = (
            timezone.now().astimezone(participant_zone).date()
            if participant_zone else timezone.localdate()
        )
        counts = dict(
            DailyCheckIn.objects.filter(
                participant_id=participant.pk,
                date__gte=today - timedelta(days=STREAK_HISTORY_DAYS),
                date__lte=today,
            ).values_list("date", "done_count")
        )
        recent_engaged = [
            count
            for day, count in sorted(counts.items(), reverse=True)
            if day <= today and count > 0
        ][:21]
        bar = _bar(recent_engaged)
        anchor = today if counts.get(today, 0) >= bar else today - timedelta(days=1)
        streak = 0
        day = anchor
        while counts.get(day, 0) >= bar:
            streak += 1
            day -= timedelta(days=1)

        participant.streak_count = streak
        participant.streak_through_date = anchor if streak else None
        participant.streak_bar = bar
        participant.streak_cache_version = STREAK_CACHE_VERSION

    if participants:
        DailyParticipant.objects.bulk_update(
            participants,
            [
                "streak_count",
                "streak_through_date",
                "streak_bar",
                "streak_cache_version",
            ],
            batch_size=500,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("daily", "0018_dismiss_support_only_mutations"),
    ]

    operations = [
        migrations.AddField(
            model_name="dailycheckin",
            name="done_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="dailyparticipant",
            name="streak_bar",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="dailyparticipant",
            name="streak_cache_version",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="dailyparticipant",
            name="streak_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="dailyparticipant",
            name="streak_through_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.RunPython(
            backfill_streak_cache,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
