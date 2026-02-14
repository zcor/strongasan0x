"""
Complete publishing workflow for weekly Roll Call
Handles all steps after ranking trials are complete
"""
import json
from django.core.management.base import BaseCommand
from django.core.management import call_command
from datetime import date, timedelta
from rollcall.models import WeeklyRollCall, RankingTrial


class Command(BaseCommand):
    help = 'Complete publishing workflow for weekly Roll Call'

    def add_arguments(self, parser):
        parser.add_argument(
            '--week-end',
            type=str,
            help='Week end date (YYYY-MM-DD). Defaults to most recent Sunday.'
        )
        parser.add_argument(
            '--substack-url',
            type=str,
            help='Substack URL (skip ode generation prompt)'
        )

    def handle(self, *args, **options):
        # Determine week
        if options['week_end']:
            week_end = date.fromisoformat(options['week_end'])
        else:
            today = date.today()
            days_since_sunday = (today.weekday() + 1) % 7
            week_end = today - timedelta(days=days_since_sunday)

        # Publication date is Monday after week end
        publication_date = week_end + timedelta(days=1)

        # Get roll call
        try:
            roll_call = WeeklyRollCall.objects.get(week_end_date=week_end)
        except WeeklyRollCall.DoesNotExist:
            self.stderr.write(f"No roll call found for week ending {week_end}")
            return

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("ROLL CALL PUBLISHING WORKFLOW")
        self.stdout.write(f"{'='*60}")
        self.stdout.write(f"Week: {roll_call.week_start_date} to {roll_call.week_end_date}")
        self.stdout.write(f"Publication date: {publication_date}")
        self.stdout.write(f"{'='*60}\n")

        # Check for ranking trials
        trials = RankingTrial.objects.filter(weekly_roll_call=roll_call)
        if not trials.exists():
            self.stderr.write("❌ No ranking trials found. Run ranking trials first:")
            self.stderr.write(f"   python manage.py run_ranking_trial --week-end {week_end} --auto-continue")
            return

        self.stdout.write(f"✅ Found {trials.count()} ranking trials")

        # Get aggregated rankings
        rankings = self._get_aggregated_rankings(trials)
        top_10 = rankings[:10]

        self.stdout.write("\nTop 10 Rankings:")
        for i, r in enumerate(top_10, 1):
            self.stdout.write(f"  {i}. {r['name']} (avg: {r['avg_rank']:.2f})")

        # Step 1: Check if we need to generate ode
        substack_url = options.get('substack_url')

        if not substack_url:
            self.stdout.write(f"\n{'='*60}")
            response = input("Generate Substack ode? (y/n, default=n): ").strip().lower()
            if response == 'y':
                self.stdout.write("\nGenerating Homeric ode...")
                call_command('generate_substack_ode', week_end=str(week_end))
                self.stdout.write("\n✅ Ode generated! Copy it to Substack, publish, then re-run with --substack-url")
                return

            # Prompt for Substack URL
            self.stdout.write(f"\n{'='*60}")
            substack_url = input("Enter Substack URL: ").strip()
            if not substack_url:
                self.stderr.write("❌ Substack URL required")
                return

        self.stdout.write(f"\n✅ Substack URL: {substack_url}")

        # Step 2: Export ranked attestations
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("STEP 1: Exporting ranked attestations...")
        attestations_file = f"attestations_{week_end}.txt"
        call_command(
            'run_ranking_trial',
            week_end=str(week_end),
            output_only=True,
            output_ranked_attestations=attestations_file
        )
        self.stdout.write(f"✅ Attestations exported to {attestations_file}")

        # Step 3: Build rankings JSON
        rankings_json = json.dumps([
            {"rank": i, "name": r['name']}
            for i, r in enumerate(top_10, 1)
        ])
        self.stdout.write(f"\nRankings JSON: {rankings_json}")

        # Step 4: Ingest roll call
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("STEP 2: Ingesting roll call...")
        call_command(
            'ingest_roll_call',
            week=str(publication_date),
            substack_url=substack_url,
            text_file=attestations_file,
            rankings=rankings_json,
            publish=True,
            overwrite=True
        )
        self.stdout.write("✅ Roll call ingested")

        # Step 5: Post to Discord
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("STEP 3: Posting to Discord...")
        try:
            call_command('post_rankings_to_discord', week=str(publication_date))
            self.stdout.write("✅ Posted to Discord")
        except Exception as e:
            self.stderr.write(f"⚠️  Discord post failed: {e}")

        # Step 6: Generate Twitter post
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("STEP 4: Generating Twitter post...")
        try:
            call_command('generate_twitter_rankings', week=str(publication_date), no_prose=True)
        except Exception as e:
            self.stderr.write(f"⚠️  Twitter generation failed: {e}")

        # Step 7: Assign Discord roles
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("STEP 5: Assigning Discord roles...")
        try:
            call_command('assign_top_ten_role')
            self.stdout.write("✅ Discord roles assigned")
        except Exception as e:
            self.stderr.write(f"⚠️  Role assignment failed: {e}")

        # Done!
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("🎉 PUBLISHING COMPLETE!")
        self.stdout.write(f"{'='*60}")
        self.stdout.write(f"Substack: {substack_url}")
        self.stdout.write(f"Week: {roll_call.week_start_date} to {roll_call.week_end_date}")
        self.stdout.write(f"{'='*60}\n")

    def _get_aggregated_rankings(self, trials):
        """Aggregate rankings from all trials"""
        from collections import defaultdict

        rank_totals = defaultdict(list)

        for trial in trials:
            rankings = trial.parsed_rankings or []
            for entry in rankings:
                name = entry.get('name')
                rank = entry.get('rank')
                if name and rank:
                    rank_totals[name].append(rank)

        avg_rankings = []
        for name, ranks in rank_totals.items():
            avg = sum(ranks) / len(ranks)
            avg_rankings.append({
                'name': name,
                'avg_rank': avg,
                'trials': len(ranks)
            })

        avg_rankings.sort(key=lambda x: x['avg_rank'])
        return avg_rankings
