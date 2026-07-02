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
    # IANA timezone name (e.g. "America/New_York"), captured from the browser
    # on app load. Blank = not yet captured → fall back to the server tz. This
    # makes the app's day boundary (today, streak, badge reset) each user's
    # LOCAL day, not the single server tz. See daily/services/tz.py.
    timezone = models.CharField(max_length=64, blank=True, default="")
    # Where this account came from — so even a stranger who self-onboards isn't
    # anonymous. source = the on-ramp ("telegram", "onboarding", ...);
    # source_detail = the human-readable origin (e.g. "@lurker_42 (tg 123)").
    source = models.CharField(max_length=20, blank=True, default="")
    source_detail = models.CharField(max_length=200, blank=True, default="")
    # Set once the user has context — either rich attestation history (stamped
    # at provision time) or a completed in-app onboarding. NULL = "naked": the
    # app runs the onboarding questionnaire on their next open. Set = skip it.
    onboarded_at = models.DateTimeField(null=True, blank=True)
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


class PushSubscription(models.Model):
    """A Web Push subscription for one device of one participant. The morning
    badge job sends a push to each active subscription so the home-screen icon
    shows today's remaining to-do count WITHOUT the app being opened (the only
    way to refresh the badge on a closed iOS PWA).

    Keyed by endpoint (unique per device+browser). A 404/410 from the push
    service means the subscription is dead — the sender deletes it.
    """

    participant = models.ForeignKey(
        DailyParticipant,
        on_delete=models.CASCADE,
        related_name="push_subscriptions",
    )
    endpoint = models.URLField(max_length=500, unique=True)
    # The two keys the push service needs to encrypt the payload (from the
    # browser's PushSubscription.toJSON().keys).
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    last_pushed_at = models.DateTimeField(null=True, blank=True)
    # The participant's LOCAL date we last sent the once-a-day "morning badge"
    # for. The hourly badge job fires the morning push at ~6am in each user's
    # OWN timezone and uses this to push exactly once per local day (so a
    # non-Pacific user's badge resets on THEIR morning, not the server's).
    last_badge_date = models.DateField(null=True, blank=True)
    # Soft fail counter — a few transient errors are fine; many = prune.
    fail_count = models.IntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=["participant"])]

    def __str__(self):
        return f"Push sub for {self.participant} ({self.endpoint[:40]}…)"


class DailyMetric(models.Model):
    """A metric DEFINITION for one participant (e.g. bodyweight, grip_left,
    back_pain). Per-participant by design: a participant with no metrics shows
    no metric UI at all — the persona branch (Spencer-the-data-logger vs.
    Amy-the-simple) falls out of whether rows exist, no flags needed.

    SCALE metrics (0-10, like pain) and number metrics (weight, grip) share one
    shape. AM/PM readings live on the reading's `slot`, not here.
    """

    KIND_NUMBER = "number"   # weight, grip — a decimal
    KIND_SCALE = "scale"     # 0-10 (pain, readiness)
    KIND_CHOICES = [(KIND_NUMBER, "Number"), (KIND_SCALE, "Scale (0-10)")]

    participant = models.ForeignKey(
        DailyParticipant, on_delete=models.CASCADE, related_name="metrics"
    )
    key = models.CharField(max_length=40)         # slug, stable
    label = models.CharField(max_length=60)       # "Bodyweight", "Grip (left)"
    unit = models.CharField(max_length=12, blank=True)  # "lbs", "/10", ""
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_NUMBER)
    # Does this metric take morning + evening readings (e.g. pain)?
    has_am_pm = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("participant", "key")]
        ordering = ["sort_order", "id"]

    def __str__(self):
        return f"{self.label} ({self.participant})"


class DailyMetricReading(models.Model):
    """One logged value for a metric on a date. `slot` distinguishes morning
    vs. evening for has_am_pm metrics ('' for single-reading metrics). Clean
    longitudinal data → charts are a trivial (date, value) read later."""

    SLOT_NONE = ""
    SLOT_AM = "am"
    SLOT_PM = "pm"

    metric = models.ForeignKey(
        DailyMetric, on_delete=models.CASCADE, related_name="readings"
    )
    date = models.DateField()
    slot = models.CharField(max_length=2, default=SLOT_NONE)  # '', 'am', 'pm'
    value = models.DecimalField(max_digits=7, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("metric", "date", "slot")]
        indexes = [models.Index(fields=["metric", "date"])]
        ordering = ["date"]

    def __str__(self):
        return f"{self.metric.label} {self.date}{('/' + self.slot) if self.slot else ''}: {self.value}"


class CoachProfile(models.Model):
    """A compact, LLM-distilled summary of a warrior's weekly Roll Call
    attestation history — the standing context the overnight coach reads so it
    coaches like it knows them, not from 5 checkboxes alone.

    Why distilled, not raw: attestation history grows without bound (the
    heaviest warrior is already ~19k tokens of logs). Injecting it raw would
    make every nightly prompt bigger and costlier forever. Instead we condense
    it ONCE per change into a bounded ~500-token profile the coach reads every
    run at a FIXED size. This is the piece that lets attestation grounding scale
    in-group: every warrior arrives from Roll Call already legible, and stays
    legible as they keep logging — with no per-user setup and flat prompt cost.

    Refresh is hash-guarded (see services: refresh_coach_profile): the profile
    is rebuilt only when the underlying attestation set actually changes, so a
    nightly coach run costs an extra LLM call only when there's new material.

    External / no-telegram participants have no attestations and thus no
    profile row — the coach falls back to today's behavior for them.
    """

    participant = models.OneToOneField(
        DailyParticipant,
        on_delete=models.CASCADE,
        related_name="coach_profile",
    )
    # The distilled summary the coach reads. Bounded by design (~500 tokens);
    # grounded ONLY in the warrior's real attestation text (no invention).
    profile_text = models.TextField()
    # Hash of the attestation set this profile was built from — lets the refresh
    # service no-op (skip the LLM call) when nothing has changed.
    source_hash = models.CharField(max_length=64, blank=True, default="")
    # How many attestations were distilled (for admin visibility / debugging).
    attestation_count = models.IntegerField(default=0)

    model_name = models.CharField(max_length=100, blank=True)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"CoachProfile for {self.participant.display_name} ({self.attestation_count} atts)"


class CoachChatMessage(models.Model):
    """One message in the live coach chat — the bidirectional successor to the
    old comment box (the #1 feedback channel). User messages are now THE way
    people talk to the coach; coach messages are its replies (including the
    migrated morning note). Full history is just an ordered query of these.
    """

    ROLE_USER = "user"
    ROLE_COACH = "coach"
    ROLE_CHOICES = [(ROLE_USER, "User"), (ROLE_COACH, "Coach")]

    participant = models.ForeignKey(
        DailyParticipant, on_delete=models.CASCADE, related_name="chat_messages"
    )
    role = models.CharField(max_length=8, choices=ROLE_CHOICES)
    text = models.TextField()
    date = models.DateField()  # the check-in day this belongs to
    # If a coach reply changed the checklist, link the suggestion that did it.
    suggestion = models.ForeignKey(
        "CoachSuggestion", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="chat_messages",
    )
    # Was a user message already DM'd to the admin? (avoid double-notify)
    notified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["participant", "created_at"])]

    def __str__(self):
        return f"{self.role} @ {self.created_at:%Y-%m-%d %H:%M}: {self.text[:40]}"
