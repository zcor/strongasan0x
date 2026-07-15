from django.core.management.base import BaseCommand, CommandError

from daily.models import DailyParticipant
from daily.services.streaks import rebuild_daily_rollups, refresh_streak_cache


class Command(BaseCommand):
    help = "Rebuild persisted daily done counts and participant streak caches."

    def add_arguments(self, parser):
        parser.add_argument(
            "--participant-id",
            type=int,
            help="Rebuild one DailyParticipant instead of every participant.",
        )

    def handle(self, *args, **options):
        participants = DailyParticipant.objects.all().order_by("id")
        participant_id = options.get("participant_id")
        if participant_id is not None:
            participants = participants.filter(pk=participant_id)
            if not participants.exists():
                raise CommandError(f"DailyParticipant {participant_id} does not exist")

        participant_count = 0
        rollup_count = 0
        for participant in participants.iterator():
            rollup_count += rebuild_daily_rollups(participant)
            refresh_streak_cache(participant)
            participant_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Rebuilt {participant_count} participant streak cache(s); "
            f"updated {rollup_count} daily rollup(s)."
        ))
