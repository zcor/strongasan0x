"""
Daily checklist app — models.

Designed for structural mutation: the question list lives in
ChecklistVersion (per participant, versioned), each check-in references
the version that was active that day, and each answer is a row in
DailyCheckInAnswer keyed by a stable question_key. Historical answers
remain queryable even after labels mutate.

Cross-app boundary: the only reference to rollcall is the optional FK
on DailyParticipant.telegram_mapping. See plan greedy-sprouting-puppy.md.
"""
import uuid

from django.db import models


BASELINE_QUESTIONS = [
    {"key": "q_water", "label": "Drank a gallon of water"},
    {"key": "q_supplements", "label": "Took vitamins / supplements"},
    {"key": "q_exercise", "label": "Got 45 minutes of exercise"},
    {"key": "q_nutrition", "label": "Met my nutrition goal"},
    {"key": "q_wins", "label": "Celebrated 2 wins"},
]


class DailyParticipant(models.Model):
    KIND_WARRIOR = "warrior"
    KIND_EXTERNAL = "external"
    KIND_CHOICES = [
        (KIND_WARRIOR, "Warrior (bridged from rollcall)"),
        (KIND_EXTERNAL, "External (token-authenticated)"),
    ]

    display_name = models.CharField(max_length=100)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    telegram_mapping = models.ForeignKey(
        "rollcall.TelegramUserMapping",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="daily_participants",
        help_text="Populated for warriors; null for external participants",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name"]
        indexes = [
            models.Index(fields=["kind", "is_active"]),
        ]

    def __str__(self):
        return f"{self.display_name} ({self.kind})"

    def get_or_create_current_checklist(self):
        current = self.checklist_versions.filter(is_current=True).first()
        if current is not None:
            return current
        return ChecklistVersion.objects.create(
            participant=self,
            questions=list(BASELINE_QUESTIONS),
            source=ChecklistVersion.SOURCE_BASELINE,
            is_current=True,
        )


class DailyAccessToken(models.Model):
    participant = models.ForeignKey(
        DailyParticipant,
        on_delete=models.CASCADE,
        related_name="access_tokens",
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        status = "revoked" if self.revoked_at else "active"
        return f"{self.participant.display_name} / {self.token} ({status})"


class ChecklistVersion(models.Model):
    """A snapshot of the question list for a participant.

    `questions` is a list of {"key": str, "label": str}. Keys are stable
    across mutations when the AI chooses to preserve an existing
    question; new questions get freshly generated keys.

    Exactly one row per participant has is_current=True at any time.
    """

    SOURCE_BASELINE = "baseline"
    SOURCE_AI_MUTATION = "ai_mutation"
    SOURCE_USER_RESET = "user_reset"
    SOURCE_CHOICES = [
        (SOURCE_BASELINE, "Baseline (Stronger in 60)"),
        (SOURCE_AI_MUTATION, "AI mutation"),
        (SOURCE_USER_RESET, "User reset to baseline"),
    ]

    participant = models.ForeignKey(
        DailyParticipant,
        on_delete=models.CASCADE,
        related_name="checklist_versions",
    )
    questions = models.JSONField(
        help_text='List of {"key": str, "label": str} dicts'
    )
    bonus_questions = models.JSONField(
        null=True,
        blank=True,
        help_text='Optional extra-credit items for the day; never counted in score',
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    derived_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="descendants",
    )
    is_current = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["participant"],
                condition=models.Q(is_current=True),
                name="one_current_checklist_per_participant",
            ),
        ]

    def __str__(self):
        return f"{self.participant.display_name} v{self.id} ({self.source})"

    def question_keys(self):
        return [q["key"] for q in self.questions]


class DailyCheckIn(models.Model):
    """One row per participant per day — envelope for the answers.

    Stores which ChecklistVersion was active on this day so the answers
    can be rendered with the right labels even after the checklist has
    since mutated.
    """

    SOURCE_WEB = "web"
    SOURCE_TELEGRAM = "telegram"
    SOURCE_CHOICES = [
        (SOURCE_WEB, "Web"),
        (SOURCE_TELEGRAM, "Telegram"),  # reserved for v2
    ]

    participant = models.ForeignKey(
        DailyParticipant,
        on_delete=models.CASCADE,
        related_name="checkins",
    )
    date = models.DateField()
    checklist_version = models.ForeignKey(
        ChecklistVersion,
        on_delete=models.PROTECT,
        related_name="checkins",
        help_text="Which checklist was active on this date",
    )
    comment = models.TextField(blank=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_WEB)
    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("participant", "date")]
        ordering = ["-date", "participant_id"]
        indexes = [
            models.Index(fields=["participant", "-date"]),
        ]

    def __str__(self):
        return f"{self.participant.display_name} {self.date.isoformat()}"

    def answers_by_key(self):
        return {a.question_key: a.state for a in self.answers.all()}

    @property
    def score(self):
        """Count of DONE among the day's CORE questions (bonus excluded)."""
        core_keys = set(self.checklist_version.question_keys())
        return sum(
            1 for a in self.answers.all()
            if a.state == DailyCheckInAnswer.STATE_DONE and a.question_key in core_keys
        )


class DailyCheckInAnswer(models.Model):
    """One state per (check_in, question_key). The label lives on
    check_in.checklist_version.questions (or bonus_questions) and is
    resolved at render time.

    States: pending (untouched — mere drift), done, skip (deliberate
    opt-out — a signal the coach treats differently from drift).
    """

    STATE_PENDING = "pending"
    STATE_DONE = "done"
    STATE_SKIP = "skip"
    STATE_CHOICES = [
        (STATE_PENDING, "Pending"),
        (STATE_DONE, "Done"),
        (STATE_SKIP, "Skipped"),
    ]

    check_in = models.ForeignKey(
        DailyCheckIn,
        on_delete=models.CASCADE,
        related_name="answers",
    )
    question_key = models.CharField(max_length=40)
    state = models.CharField(max_length=10, choices=STATE_CHOICES, default=STATE_PENDING)

    class Meta:
        unique_together = [("check_in", "question_key")]
        ordering = ["check_in", "question_key"]


class CoachSuggestion(models.Model):
    """AI-generated suggestion. Optionally carries a proposed mutation
    (proposed_questions); if set, will be auto-applied the next day
    unless the user dismissed the suggestion.
    """

    STATUS_PENDING = "pending"
    STATUS_SHOWN = "shown"
    STATUS_ACKNOWLEDGED = "acknowledged"
    STATUS_DISMISSED = "dismissed"
    STATUS_APPLIED = "applied"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SHOWN, "Shown"),
        (STATUS_ACKNOWLEDGED, "Acknowledged"),
        (STATUS_DISMISSED, "Dismissed"),
        (STATUS_APPLIED, "Mutation applied"),
    ]

    check_in = models.ForeignKey(
        DailyCheckIn,
        on_delete=models.CASCADE,
        related_name="suggestions",
    )
    suggestion_text = models.TextField()
    proposed_questions = models.JSONField(
        null=True,
        blank=True,
        help_text='Optional new question list to auto-apply tomorrow',
    )
    proposed_bonus = models.JSONField(
        null=True,
        blank=True,
        help_text='Optional extra-credit items for tomorrow (0-3)',
    )
    rationale = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)

    model_name = models.CharField(max_length=100, blank=True)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    applied_version = models.ForeignKey(
        ChecklistVersion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_from_suggestions",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["check_in", "status"]),
        ]

    def __str__(self):
        return f"Suggestion for {self.check_in} ({self.status})"
