from django.db import models
from django.contrib.auth.models import User


class WeeklyRollCall(models.Model):
    """Store weekly Substack roll call posts"""
    week_start_date = models.DateField(help_text="Monday of the week")
    week_end_date = models.DateField(help_text="Sunday of the week")
    substack_url = models.URLField(help_text="Link to Substack post")
    full_text = models.TextField(help_text="Complete Substack post text (unlimited length)")
    is_published = models.BooleanField(default=False, help_text="Whether this roll call has been officially published.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Weekly Roll Call"
        verbose_name_plural = "Weekly Roll Calls"
        unique_together = ['week_start_date']
        ordering = ['-week_start_date']

    def __str__(self):
        return f"Week of {self.week_start_date.strftime('%B %d, %Y')}"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.week_start_date.weekday() != 0:  # Monday is 0
            raise ValidationError({'week_start_date': 'Week start date must be a Monday'})
        if self.week_end_date.weekday() != 6:  # Sunday is 6
            raise ValidationError({'week_end_date': 'Week end date must be a Sunday'})
        if (self.week_end_date - self.week_start_date).days != 6:
            raise ValidationError('Week end date must be exactly 6 days after week start date')


class RollCallRanking(models.Model):
    """Store rankings for each weekly roll call"""
    weekly_roll_call = models.ForeignKey(WeeklyRollCall, on_delete=models.CASCADE, related_name='rankings')
    rank = models.PositiveIntegerField(help_text="Position in rankings (1, 2, 3, etc.)")
    name = models.CharField(max_length=100, help_text="Display name")
    twitter_handle = models.CharField(max_length=100, blank=True, help_text="Twitter/X handle (without @)")

    class Meta:
        verbose_name = "Roll Call Ranking"
        verbose_name_plural = "Roll Call Rankings"
        unique_together = ['weekly_roll_call', 'rank']
        ordering = ['rank']

    def __str__(self):
        return f"Rank {self.rank}: {self.name}"


class DiscordUserMapping(models.Model):
    """Link Discord users to contest participants (by name or Twitter handle)"""
    discord_user_id = models.BigIntegerField(unique=True, help_text="Discord user ID")
    discord_username = models.CharField(max_length=100, help_text="Discord username")
    discord_display_name = models.CharField(max_length=100, blank=True, help_text="Discord display name")
    linked_name = models.CharField(max_length=100, blank=True, help_text="Links to RollCallRanking.name")
    linked_twitter_handle = models.CharField(max_length=100, blank=True, help_text="Links to RollCallRanking.twitter_handle")
    linked_at = models.DateTimeField(auto_now_add=True, help_text="When the link was created")
    is_active = models.BooleanField(default=True, help_text="Whether this mapping is active")
    # Link to Django User for web authentication
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='discord_mappings', help_text="Linked Django user account")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Discord User Mapping"
        verbose_name_plural = "Discord User Mappings"
        ordering = ['discord_username']
        indexes = [
            models.Index(fields=['linked_name']),
            models.Index(fields=['linked_twitter_handle']),
            models.Index(fields=['is_active']),
            models.Index(fields=['user']),
        ]

    def __str__(self):
        if self.linked_name:
            return f"{self.discord_username} → {self.linked_name}"
        return f"{self.discord_username} (unlinked)"


class TelegramUserMapping(models.Model):
    """Link Telegram users to contest participants (by name or Twitter handle)"""
    telegram_user_id = models.BigIntegerField(unique=True, help_text="Telegram user ID")
    telegram_username = models.CharField(max_length=100, blank=True, help_text="Telegram username")
    telegram_first_name = models.CharField(max_length=100, help_text="Telegram first name")
    telegram_last_name = models.CharField(max_length=100, blank=True, help_text="Telegram last name")
    linked_name = models.CharField(max_length=100, blank=True, help_text="Links to RollCallRanking.name")
    linked_twitter_handle = models.CharField(max_length=100, blank=True, help_text="Links to RollCallRanking.twitter_handle")
    linked_at = models.DateTimeField(auto_now_add=True, help_text="When the link was created")
    is_active = models.BooleanField(default=True, help_text="Whether this mapping is active")
    # Link to Django User for web authentication
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='telegram_mappings', help_text="Linked Django user account")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Telegram User Mapping"
        verbose_name_plural = "Telegram User Mappings"
        ordering = ['telegram_first_name', 'telegram_username']
        indexes = [
            models.Index(fields=['linked_name']),
            models.Index(fields=['linked_twitter_handle']),
            models.Index(fields=['is_active']),
            models.Index(fields=['user']),
        ]

    def __str__(self):
        display_name = f"{self.telegram_first_name} {self.telegram_last_name}".strip() or self.telegram_username or f"User {self.telegram_user_id}"
        if self.linked_name:
            return f"{display_name} → {self.linked_name}"
        return f"{display_name} (unlinked)"


class Attestation(models.Model):
    """Store weekly attestations from Discord and Telegram"""
    SOURCE_CHOICES = [
        ('discord', 'Discord'),
        ('telegram', 'Telegram'),
        ('strava', 'Strava'),
    ]

    weekly_roll_call = models.ForeignKey(WeeklyRollCall, on_delete=models.CASCADE, related_name='attestations')
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='discord', help_text="Platform where attestation was posted")

    # Discord fields (nullable for Telegram attestations)
    discord_user = models.ForeignKey(DiscordUserMapping, on_delete=models.SET_NULL, null=True, blank=True, related_name='attestations')
    discord_message_id = models.BigIntegerField(null=True, blank=True, help_text="Discord message ID")
    discord_channel_id = models.BigIntegerField(null=True, blank=True, help_text="Discord channel ID where attestation was posted")

    # Telegram fields (nullable for Discord attestations)
    telegram_user = models.ForeignKey(TelegramUserMapping, on_delete=models.SET_NULL, null=True, blank=True, related_name='attestations')
    telegram_message_id = models.BigIntegerField(null=True, blank=True, help_text="Telegram message ID")
    telegram_chat_id = models.BigIntegerField(null=True, blank=True, help_text="Telegram chat ID")

    # Multi-part attestation support
    parent_attestation = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='parts', help_text="Parent attestation for multi-part attestations")
    part_number = models.IntegerField(default=1, help_text="Part number for multi-part attestations")

    # Content
    raw_text = models.TextField(help_text="Full attestation text")
    posted_at = models.DateTimeField(help_text="When the attestation was posted")
    has_attachments = models.BooleanField(default=False, help_text="Whether this part has attachments")
    attachment_count = models.IntegerField(default=0, help_text="Number of attachments")

    # Review status
    is_hidden = models.BooleanField(default=False, help_text="Hidden from review/ranking (soft delete)")
    hidden_at = models.DateTimeField(null=True, blank=True, help_text="When the attestation was hidden")

    # Metadata
    parsed_data = models.JSONField(default=dict, blank=True, null=True, help_text="Parsed attestation data (for future use)")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Attestation"
        verbose_name_plural = "Attestations"
        ordering = ['-posted_at', 'part_number']
        indexes = [
            models.Index(fields=['weekly_roll_call', 'posted_at']),
            models.Index(fields=['discord_user', 'posted_at']),
            models.Index(fields=['telegram_user', 'posted_at']),
            models.Index(fields=['source', 'discord_message_id']),
            models.Index(fields=['source', 'telegram_chat_id', 'telegram_message_id']),
            models.Index(fields=['parent_attestation', 'part_number']),
            models.Index(fields=['is_hidden']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['source', 'discord_message_id'], condition=models.Q(source='discord', discord_message_id__isnull=False), name='unique_discord_message'),
            models.UniqueConstraint(
                fields=['source', 'telegram_chat_id', 'telegram_message_id'],
                condition=models.Q(source='telegram', telegram_message_id__isnull=False, telegram_chat_id__isnull=False),
                name='unique_telegram_chat_message',
            ),
        ]

    def __str__(self):
        if self.source == 'discord':
            user_name = self.discord_user.discord_username if self.discord_user else "Unknown"
        else:
            user_name = (self.telegram_user.telegram_first_name if self.telegram_user else "Unknown")

        part_str = f" (Part {self.part_number})" if self.parent_attestation else ""
        return f"Attestation from {user_name} - {self.weekly_roll_call.week_start_date}{part_str}"

    @property
    def user_mapping(self):
        """Get the appropriate user mapping regardless of source"""
        return self.discord_user if self.source == 'discord' else self.telegram_user

    @property
    def message_id(self):
        """Get the appropriate message ID regardless of source"""
        return self.discord_message_id if self.source == 'discord' else self.telegram_message_id


class MessageLog(models.Model):
    """Store all messages from Discord and Telegram for later review and conversion to attestations"""
    SOURCE_CHOICES = [
        ('discord', 'Discord'),
        ('telegram', 'Telegram'),
    ]

    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, help_text="Platform where message was posted")

    # Discord fields (nullable for Telegram messages)
    discord_user = models.ForeignKey(DiscordUserMapping, on_delete=models.SET_NULL, null=True, blank=True, related_name='message_logs')
    discord_message_id = models.BigIntegerField(null=True, blank=True, unique=True, help_text="Discord message ID")
    discord_channel_id = models.BigIntegerField(null=True, blank=True, help_text="Discord channel ID")

    # Telegram fields (nullable for Discord messages)
    telegram_user = models.ForeignKey(TelegramUserMapping, on_delete=models.SET_NULL, null=True, blank=True, related_name='message_logs')
    telegram_message_id = models.BigIntegerField(null=True, blank=True, help_text="Telegram message ID (unique within a chat, not globally)")
    telegram_chat_id = models.BigIntegerField(null=True, blank=True, help_text="Telegram chat ID")

    # Content
    content = models.TextField(help_text="Message content")
    posted_at = models.DateTimeField(help_text="When the message was posted")
    has_attachments = models.BooleanField(default=False, help_text="Whether message has attachments")
    attachment_count = models.IntegerField(default=0, help_text="Number of attachments")
    attachment_info = models.JSONField(default=dict, blank=True, help_text="Details about attachments (file IDs, names, types, etc.)")

    # Metadata
    is_attestation = models.BooleanField(default=False, help_text="Whether this message was converted to an attestation")
    attestation = models.ForeignKey(Attestation, on_delete=models.SET_NULL, null=True, blank=True, related_name='source_messages', help_text="Attestation created from this message")

    # Conversational bot upgrade (Phase 0)
    is_bot_reply = models.BooleanField(default=False, db_index=True, help_text="True for messages the bot itself sent (vs warrior-authored)")
    classifier_verdict = models.JSONField(null=True, blank=True, help_text="Sonnet classifier output (Phase A) — {is_attestation, should_reply, intent, ...}")
    kind = models.CharField(max_length=32, default='warrior', help_text="Row purpose: 'warrior', 'reply', 'attestation_ack', 'command_reply', 'error', 'private_files_listing', etc.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Message Log"
        verbose_name_plural = "Message Logs"
        ordering = ['-posted_at']
        indexes = [
            models.Index(fields=['source', 'posted_at']),
            models.Index(fields=['discord_user', 'posted_at']),
            models.Index(fields=['telegram_user', 'posted_at']),
            models.Index(fields=['source', 'discord_message_id']),
            models.Index(fields=['source', 'telegram_chat_id', 'telegram_message_id']),
            models.Index(fields=['is_attestation']),
            models.Index(fields=['posted_at']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['source', 'discord_message_id'], condition=models.Q(source='discord', discord_message_id__isnull=False), name='unique_discord_message_log'),
            models.UniqueConstraint(
                fields=['source', 'telegram_chat_id', 'telegram_message_id'],
                condition=models.Q(source='telegram', telegram_message_id__isnull=False, telegram_chat_id__isnull=False),
                name='unique_telegram_chat_message_log',
            ),
        ]

    def __str__(self):
        if self.source == 'discord':
            user_name = self.discord_user.discord_username if self.discord_user else "Unknown"
        else:
            user_name = (self.telegram_user.telegram_first_name if self.telegram_user else "Unknown")

        attestation_str = " (attestation)" if self.is_attestation else ""
        return f"Message from {user_name} - {self.posted_at.strftime('%Y-%m-%d %H:%M')}{attestation_str}"

    @property
    def user_mapping(self):
        """Get the appropriate user mapping regardless of source"""
        return self.discord_user if self.source == 'discord' else self.telegram_user

    @property
    def message_id(self):
        """Get the appropriate message ID regardless of source"""
        return self.discord_message_id if self.source == 'discord' else self.telegram_message_id


class ChatContextReset(models.Model):
    """Marks a point in time before which messages in a chat are excluded from
    the conversational bot's context window. /forget writes one of these rows;
    no MessageLog rows are mutated or deleted. Single source of truth for
    context cutoffs — see plan binary-juggling-locket.md."""
    SOURCE_CHOICES = [
        ('telegram', 'Telegram'),
        ('discord', 'Discord'),
    ]
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='telegram', help_text="Platform")
    chat_id = models.BigIntegerField(help_text="Telegram chat ID or Discord channel ID")
    cutoff_at = models.DateTimeField(help_text="Messages with posted_at <= cutoff_at are hidden from bot context")
    requested_by = models.ForeignKey(TelegramUserMapping, on_delete=models.SET_NULL, null=True, blank=True, related_name='context_resets', help_text="User who issued /forget")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Chat Context Reset"
        verbose_name_plural = "Chat Context Resets"
        ordering = ['-cutoff_at']
        indexes = [
            models.Index(fields=['source', 'chat_id', '-cutoff_at']),
        ]

    def __str__(self):
        return f"Reset {self.source}/{self.chat_id} @ {self.cutoff_at.isoformat()}"

    @classmethod
    def latest_reset_for(cls, chat_id, source='telegram'):
        """Return the most recent cutoff datetime for a chat, or None if no reset exists."""
        latest = cls.objects.filter(source=source, chat_id=chat_id).order_by('-cutoff_at').first()
        return latest.cutoff_at if latest else None


class WebLoginToken(models.Model):
    """Temporary tokens for web login via Telegram bot"""
    token = models.CharField(max_length=32, unique=True, help_text="Random login token")
    telegram_user = models.ForeignKey(TelegramUserMapping, on_delete=models.CASCADE, null=True, blank=True, help_text="Telegram user who confirmed this token")
    is_used = models.BooleanField(default=False, help_text="Whether token has been used for login")
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True, help_text="When user confirmed via Telegram")
    expires_at = models.DateTimeField(help_text="When this token expires")

    class Meta:
        verbose_name = "Web Login Token"
        verbose_name_plural = "Web Login Tokens"
        ordering = ['-created_at']

    def __str__(self):
        status = "used" if self.is_used else ("confirmed" if self.telegram_user else "pending")
        return f"Token {self.token[:8]}... ({status})"

    @classmethod
    def create_token(cls):
        """Create a new login token"""
        import secrets
        from django.utils import timezone
        from datetime import timedelta

        token = secrets.token_hex(8)  # 16 character hex token
        expires_at = timezone.now() + timedelta(minutes=10)

        return cls.objects.create(token=token, expires_at=expires_at)

    def is_valid(self):
        """Check if token is still valid"""
        from django.utils import timezone
        return not self.is_used and timezone.now() < self.expires_at

    def confirm(self, telegram_user):
        """Confirm this token with a Telegram user"""
        from django.utils import timezone
        self.telegram_user = telegram_user
        self.confirmed_at = timezone.now()
        self.save()


class RankingTrial(models.Model):
    """Store AI ranking trial results for weekly roll calls"""
    PROVIDER_CHOICES = [
        ('openai', 'OpenAI'),
        ('anthropic', 'Anthropic'),
        ('grok', 'Grok'),
        ('deepseek', 'DeepSeek'),
    ]

    weekly_roll_call = models.ForeignKey(WeeklyRollCall, on_delete=models.CASCADE, related_name='ranking_trials')
    trial_number = models.IntegerField(help_text="Sequential trial number for the week")
    ai_provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES, help_text="AI provider used")
    ai_model = models.CharField(max_length=100, help_text="Specific model used (e.g., 'gpt-4', 'claude-3-opus')")
    raw_response = models.TextField(help_text="Full API response for debugging")
    parsed_rankings = models.JSONField(default=list, help_text="Parsed ranking data")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Ranking Trial"
        verbose_name_plural = "Ranking Trials"
        ordering = ['trial_number']
        indexes = [
            models.Index(fields=['weekly_roll_call', 'trial_number']),
            models.Index(fields=['weekly_roll_call']),
            models.Index(fields=['trial_number']),
        ]
        unique_together = ['weekly_roll_call', 'trial_number']

    def __str__(self):
        return f"Trial {self.trial_number} - {self.ai_provider} ({self.ai_model}) - Week of {self.weekly_roll_call.week_start_date}"


class StravaAuth(models.Model):
    """Store Strava OAuth tokens linked to Telegram users"""
    telegram_user = models.OneToOneField(
        TelegramUserMapping,
        on_delete=models.CASCADE,
        related_name='strava_auth',
        help_text="Telegram user who linked this Strava account"
    )
    athlete_id = models.BigIntegerField(help_text="Strava athlete ID")
    access_token = models.CharField(max_length=255, help_text="OAuth access token")
    refresh_token = models.CharField(max_length=255, help_text="OAuth refresh token")
    token_expires_at = models.DateTimeField(help_text="When the access token expires")
    athlete_data = models.JSONField(default=dict, help_text="Cached athlete profile data")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Strava Authentication"
        verbose_name_plural = "Strava Authentications"

    def __str__(self):
        athlete_name = self.athlete_data.get('firstname', '') or f"Athlete {self.athlete_id}"
        return f"Strava: {athlete_name} ({self.telegram_user})"

    def is_token_expired(self):
        """Check if the access token has expired"""
        from django.utils import timezone
        return timezone.now() >= self.token_expires_at


class StravaActivity(models.Model):
    """Store Strava activities for users"""
    strava_auth = models.ForeignKey(
        StravaAuth,
        on_delete=models.CASCADE,
        related_name='activities',
        help_text="Strava auth this activity belongs to"
    )
    activity_id = models.BigIntegerField(unique=True, help_text="Strava activity ID")
    name = models.CharField(max_length=255, help_text="Activity name")
    sport_type = models.CharField(max_length=100, help_text="Sport type (Run, Ride, Swim, etc.)")
    start_date = models.DateTimeField(help_text="Activity start time (UTC)")
    start_date_local = models.DateTimeField(help_text="Activity start time (local timezone)")
    moving_time_seconds = models.IntegerField(help_text="Time spent moving in seconds")
    elapsed_time_seconds = models.IntegerField(help_text="Total elapsed time in seconds")
    distance_meters = models.FloatField(null=True, blank=True, help_text="Distance in meters")
    total_elevation_gain = models.FloatField(null=True, blank=True, help_text="Elevation gain in meters")
    average_speed = models.FloatField(null=True, blank=True, help_text="Average speed in m/s")
    max_speed = models.FloatField(null=True, blank=True, help_text="Max speed in m/s")
    average_heartrate = models.FloatField(null=True, blank=True, help_text="Average heart rate")
    max_heartrate = models.FloatField(null=True, blank=True, help_text="Max heart rate")
    average_cadence = models.FloatField(null=True, blank=True, help_text="Average cadence")
    average_watts = models.FloatField(null=True, blank=True, help_text="Average power in watts")
    suffer_score = models.IntegerField(null=True, blank=True, help_text="Relative effort score")
    kudos_count = models.IntegerField(default=0, help_text="Number of kudos")
    raw_data = models.JSONField(default=dict, help_text="Full API response")
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Strava Activity"
        verbose_name_plural = "Strava Activities"
        ordering = ['-start_date']
        indexes = [
            models.Index(fields=['strava_auth', 'start_date']),
            models.Index(fields=['sport_type']),
        ]

    def __str__(self):
        return f"{self.name} - {self.sport_type} - {self.start_date_local.strftime('%Y-%m-%d')}"

    @property
    def distance_miles(self):
        """Convert distance to miles"""
        if self.distance_meters:
            return self.distance_meters / 1609.344
        return None

    @property
    def distance_km(self):
        """Convert distance to kilometers"""
        if self.distance_meters:
            return self.distance_meters / 1000
        return None

    @property
    def moving_time_formatted(self):
        """Format moving time as HH:MM:SS or MM:SS"""
        hours = self.moving_time_seconds // 3600
        minutes = (self.moving_time_seconds % 3600) // 60
        seconds = self.moving_time_seconds % 60
        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    @property
    def elevation_gain_feet(self):
        """Convert elevation gain to feet"""
        if self.total_elevation_gain:
            return self.total_elevation_gain * 3.28084
        return None


class ExtractedMetrics(models.Model):
    """AI-extracted structured metrics from attestation freeform text."""
    attestation = models.OneToOneField('Attestation', on_delete=models.CASCADE, related_name='metrics')
    extracted_at = models.DateTimeField(auto_now_add=True)
    model_used = models.CharField(max_length=100)
    raw_response = models.JSONField(default=dict)

    # Fitness (all nullable — only populated if mentioned in attestation)
    daily_steps = models.IntegerField(null=True, blank=True)
    calories_burned = models.IntegerField(null=True, blank=True)
    resting_heart_rate = models.IntegerField(null=True, blank=True)
    vo2_max = models.FloatField(null=True, blank=True)
    sleep_hours = models.FloatField(null=True, blank=True)
    body_weight = models.FloatField(null=True, blank=True)
    body_fat_pct = models.FloatField(null=True, blank=True)

    # Training counts
    strength_sessions = models.IntegerField(null=True, blank=True)
    cardio_sessions = models.IntegerField(null=True, blank=True)
    combat_sessions = models.IntegerField(null=True, blank=True)
    total_training_sessions = models.IntegerField(null=True, blank=True)

    # Nutrition (daily avg)
    protein_grams = models.IntegerField(null=True, blank=True)
    calories_consumed = models.IntegerField(null=True, blank=True)

    # Key lifts (working weight or 1RM as reported)
    bench_press = models.FloatField(null=True, blank=True)
    squat = models.FloatField(null=True, blank=True)
    deadlift = models.FloatField(null=True, blank=True)

    # Catch-all for warrior-specific metrics
    extra_metrics = models.JSONField(default=dict, blank=True)

    # Extraction status tracking
    extraction_error = models.TextField(blank=True, default='')
    last_extraction_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Extracted Metrics"
        verbose_name_plural = "Extracted Metrics"
        indexes = [
            models.Index(fields=['attestation']),
        ]

    def __str__(self):
        return f"Metrics for {self.attestation}"
