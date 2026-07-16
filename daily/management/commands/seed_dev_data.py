"""Seed local dev participants with data for every beta UI section.

Creates (or fully re-seeds) two beta participants so both focus modes can be
tested side by side:

  - "Dev Health" (focus=health)
  - "Dev Life"   (focus=life)

Each gets: weekly habits (custom days, small steps with any/all rules, a
bonus item), active North Stars with open and done steps, one-off wins,
a selected Today's Win, achieved and archived North Stars, ten days of
check-in history (streak + week strip), Jamie chat history, and a pending
morning report note.

Usage:
    python manage.py seed_dev_data            # create/re-seed both users
    python manage.py seed_dev_data --naked    # also add "Dev Naked" (tests onboarding)

Re-running wipes and re-seeds only these participants' DATA (matched by
source='seed'); real participants are never touched. The participants and
their access tokens persist across runs, so the printed login URLs, and any
phone already signed in with them, stay valid forever.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from daily.models import (
    ChecklistVersion,
    CoachChatMessage,
    CoachSuggestion,
    DailyAccessToken,
    DailyCheckIn,
    DailyCheckInAnswer,
    DailyParticipant,
    WinItem,
)
from daily.services.streaks import refresh_streak_cache, update_checkin_done_count
from daily.services.wins import (
    add_win,
    archive_goal,
    complete_goal,
    complete_win,
    create_north_star,
    select_todays_win,
)

SEED_SOURCE = "seed"

HEALTH_QUESTIONS = [
    {
        "key": "u_strength",
        "label": "Strength training",
        "days": [0, 2, 4],  # Mon/Wed/Fri
        "step_rule": "any",
        "items": [
            {"key": "u_bench", "label": "Bench press"},
            {"key": "u_squats", "label": "Squats"},
            {"key": "u_rows", "label": "Rows"},
        ],
    },
    {"key": "u_water", "label": "Drink a gallon of water"},
    {
        "key": "u_mobility",
        "label": "Morning mobility",
        "days": [1, 3],  # Tue/Thu
        "step_rule": "all",
        "items": [
            {"key": "u_stretch", "label": "10 min stretch"},
            {"key": "u_foam", "label": "Foam roll"},
        ],
    },
    {"key": "u_walk", "label": "Evening walk", "days": [5, 6]},  # Sat/Sun
]
# Beta shows only explicitly health-tagged bonuses (the safety scope applies
# to both focuses), so every seeded bonus must carry category=health.
HEALTH_BONUS = [{"key": "bonus_veg", "label": "Bonus: a vegetable with every meal", "category": "health"}]

LIFE_QUESTIONS = [
    {"key": "u_read", "label": "Read 20 minutes"},
    {
        "key": "u_spanish",
        "label": "Practice Spanish",
        "days": [0, 1, 2, 3, 4],  # weekdays
        "step_rule": "any",
        "items": [
            {"key": "u_duolingo", "label": "One Duolingo lesson"},
            {"key": "u_flashcards", "label": "10 flashcards"},
        ],
    },
    {"key": "u_friend", "label": "Reach out to a friend", "days": [5, 6]},
    {
        "key": "u_tidy",
        "label": "Ten-minute tidy",
        "step_rule": "all",
        "items": [
            {"key": "u_desk", "label": "Clear the desk"},
            {"key": "u_inbox", "label": "Inbox to zero-ish"},
        ],
    },
]
LIFE_BONUS = [{"key": "bonus_stretch", "label": "Bonus: stretch for five minutes", "category": "health"}]

HEALTH_WINS = {
    "goals": [
        ("Run a 10k", ["Sign up for a race", "Run 3k without stopping", "Run 5k", "Run 8k"]),
        ("Fix my sleep", ["No screens after 10pm for a week", "Buy blackout curtains"]),
    ],
    "singles": ["Book the dentist appointment", "Try the new climbing gym", "Meal prep Sunday"],
    "achieved": [
        ("Deadlift bodyweight", ["Learn the form", "Hit 60kg", "Hit bodyweight"]),
        ("30-day yoga streak", ["Pick a follow-along series", "First full week", "Day 30"]),
        ("Quit soda", ["Swap lunch soda for sparkling water", "Two clean weeks"]),
    ],
    "archived": [
        ("Cold plunges", ["Buy a tub", "First 2-minute plunge"]),
        ("Train for a triathlon", ["Join a swim class", "Buy a road bike"]),
    ],
}
LIFE_WINS = {
    "goals": [
        ("Get a new job", ["Update the resume", "Reach out to 3 contacts", "Apply to 5 roles"]),
        ("Write a short story", ["Outline the plot", "Write the first page"]),
    ],
    "singles": ["Renew the passport", "Frame the prints", "Plan mom's birthday"],
    "achieved": [
        ("Declutter the garage", ["Sort into keep/donate", "Donation run"]),
        ("File the taxes early", ["Gather the documents", "Book the accountant"]),
        ("Read 5 books this quarter", ["Pick the list", "Books 1-3", "Books 4-5"]),
    ],
    "archived": [
        ("Learn the guitar", ["Buy a used guitar", "First chord drill"]),
        ("Start a podcast", ["Outline 3 episodes", "Test the mic setup"]),
    ],
}

CHAT_SCRIPT = [
    ("user", "Feeling a bit flat this week, honestly.", 1),
    ("coach", "That's okay, flat weeks happen. You still showed up 3 of the "
              "last 4 days, and that counts. Want to keep today small?", 1),
    ("user", "Yeah, let's keep it small today.", 0),
    ("coach", "Small it is. One step on your win and whatever habits fit. "
              "I'm here to cheer you on.", 0),
]


class Command(BaseCommand):
    help = "Seed 'Dev Health' and 'Dev Life' beta participants with data for every UI section."

    def add_arguments(self, parser):
        parser.add_argument("--naked", action="store_true",
                            help="Also create 'Dev Naked' (no data, not onboarded) to test beta onboarding.")

    def handle(self, *args, **opts):
        today = timezone.localdate()
        specs = [
            ("Dev Health", DailyParticipant.FOCUS_HEALTH, HEALTH_QUESTIONS, HEALTH_BONUS, HEALTH_WINS),
            ("Dev Life", DailyParticipant.FOCUS_LIFE, LIFE_QUESTIONS, LIFE_BONUS, LIFE_WINS),
        ]
        for name, focus, questions, bonus, wins in specs:
            participant = self._fresh_participant(name, focus)
            version = ChecklistVersion.objects.create(
                participant=participant,
                questions=questions,
                bonus_questions=bonus,
                source=ChecklistVersion.SOURCE_BASELINE,
                is_current=True,
            )
            self._seed_wins(participant, wins, today)
            yesterday_checkin = self._seed_history(participant, version, questions, today)
            self._seed_jamie(participant, yesterday_checkin, name, today)
            refresh_streak_cache(participant, today=today)
            url = self._login_url(participant)
            self.stdout.write(self.style.SUCCESS(f"{name}: http://localhost:8001{url}"))

        if opts["naked"]:
            participant = self._fresh_participant("Dev Naked", focus="", onboarded=False)
            url = self._login_url(participant)
            self.stdout.write(self.style.SUCCESS(f"Dev Naked (onboarding): http://localhost:8001{url}"))

    # ------------------------------------------------------------------
    def _fresh_participant(self, name, focus, onboarded=True):
        """Reuse the participant across runs (stable id, login link, and phone
        session); wipe only its data."""
        participant, _ = DailyParticipant.objects.get_or_create(
            display_name=name,
            source=SEED_SOURCE,
            defaults={
                "kind": DailyParticipant.KIND_EXTERNAL,
                "source_detail": "seed_dev_data",
            },
        )
        # Check-ins PROTECT their checklist version, so clear them first.
        participant.checkins.all().delete()
        participant.checklist_versions.all().delete()
        participant.wins.all().delete()
        participant.chat_messages.all().delete()
        participant.beta = True
        participant.ai_mutations_enabled = False
        participant.focus = focus
        participant.onboarded_at = timezone.now() if onboarded else None
        participant.streak_count = 0
        participant.streak_through_date = None
        participant.save()
        return participant

    def _seed_wins(self, participant, wins, today):
        # Active North Stars: first has progress (one step already done).
        first_goal = None
        for index, (goal_text, stones) in enumerate(wins["goals"]):
            goal = create_north_star(participant, goal_text, stones)
            if index == 0 and goal is not None:
                first_goal = goal
                complete_win(goal.stones.order_by("order").first())

        for text in wins["singles"]:
            add_win(participant, text)

        # Today's Win = the first open step of the first goal.
        if first_goal is not None:
            step = first_goal.stones.filter(status=WinItem.STATUS_OPEN).order_by("order").first()
            if step is not None:
                select_todays_win(participant, step, today)

        # Completed wins on past days light up the week strip.
        for days_ago, text in ((2, "Cleared the email backlog"), (4, "Fixed the squeaky door")):
            win = add_win(participant, text)
            if win is not None:
                complete_win(win, featured_on=today - timedelta(days=days_ago))

        # Achieved North Stars (all steps checked, then the summit).
        for goal_text, stones in wins["achieved"]:
            achieved = create_north_star(participant, goal_text, stones)
            if achieved is not None:
                for stone in achieved.stones.all():
                    complete_win(stone)
                complete_goal(achieved)

        # Archived North Stars.
        for goal_text, stones in wins["archived"]:
            archived = create_north_star(participant, goal_text, stones)
            if archived is not None:
                archive_goal(archived)

    def _seed_history(self, participant, version, questions, today):
        """Ten days of check-ins: mostly good, one rest day, today partial."""
        yesterday_checkin = None
        for days_ago in range(9, -1, -1):
            day = today - timedelta(days=days_ago)
            if days_ago == 6:  # one missed day, so the streak logic has texture
                continue
            checkin = DailyCheckIn.objects.create(
                participant=participant, date=day, checklist_version=version,
            )
            # Habits scheduled for this weekday; today stays partially done.
            scheduled = [
                q for q in questions
                if day.weekday() in q.get("days", [0, 1, 2, 3, 4, 5, 6])
            ]
            done = scheduled if days_ago > 0 else scheduled[:1]
            for question in done:
                DailyCheckInAnswer.objects.create(
                    check_in=checkin,
                    question_key=question["key"],
                    state=DailyCheckInAnswer.STATE_DONE,
                )
                for item in question.get("items", [])[:1]:
                    DailyCheckInAnswer.objects.create(
                        check_in=checkin,
                        question_key=item["key"],
                        state=DailyCheckInAnswer.STATE_DONE,
                    )
            update_checkin_done_count(checkin)
            if days_ago == 1:
                yesterday_checkin = checkin
        return yesterday_checkin

    def _seed_jamie(self, participant, yesterday_checkin, name, today):
        if yesterday_checkin is not None:
            CoachSuggestion.objects.create(
                check_in=yesterday_checkin,
                suggestion_text=(
                    f"Morning! Yesterday you kept the chain alive, {name.split()[-1]} "
                    "mode suits you. Your Today's Win is already picked, so start "
                    "there and let the habits follow. I'm here to cheer you on."
                ),
                rationale=CoachSuggestion.RATIONALE_DAILY_REPORT,
                status=CoachSuggestion.STATUS_PENDING,
                model_name="seed",
            )
        for role, text, days_ago in CHAT_SCRIPT:
            CoachChatMessage.objects.create(
                participant=participant,
                role=role,
                text=text,
                date=today - timedelta(days=days_ago),
            )

    def _login_url(self, participant):
        token = participant.access_tokens.filter(revoked_at__isnull=True).first()
        if token is None:
            token = DailyAccessToken.objects.create(participant=participant)
        return f"/daily/c/{token.token}/"
