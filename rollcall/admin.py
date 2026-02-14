from django.contrib import admin
from .models import (
    WeeklyRollCall, RollCallRanking,
    DiscordUserMapping, TelegramUserMapping, Attestation, RankingTrial,
    MessageLog, WebLoginToken, StravaAuth, StravaActivity
)


class RollCallRankingInline(admin.TabularInline):
    """Inline admin for rankings within WeeklyRollCall"""
    model = RollCallRanking
    extra = 6
    fields = ['rank', 'name', 'twitter_handle']


class AttestationInline(admin.TabularInline):
    """Inline admin for attestations within WeeklyRollCall"""
    model = Attestation
    extra = 0
    fields = ['discord_user', 'telegram_user', 'source', 'posted_at', 'raw_text']
    readonly_fields = ['posted_at']
    can_delete = False


@admin.register(WeeklyRollCall)
class WeeklyRollCallAdmin(admin.ModelAdmin):
    list_display = ['week_start_date', 'week_end_date', 'is_published', 'substack_url', 'created_at']
    list_filter = ['is_published', 'week_start_date']
    date_hierarchy = 'week_start_date'
    search_fields = ['substack_url', 'full_text']
    fields = ['week_start_date', 'week_end_date', 'substack_url', 'full_text', 'is_published']
    inlines = [RollCallRankingInline, AttestationInline]


@admin.register(RollCallRanking)
class RollCallRankingAdmin(admin.ModelAdmin):
    list_display = ['weekly_roll_call', 'rank', 'name', 'twitter_handle']
    list_filter = ['weekly_roll_call', 'rank']
    search_fields = ['name', 'twitter_handle']


@admin.register(DiscordUserMapping)
class DiscordUserMappingAdmin(admin.ModelAdmin):
    list_display = ['discord_username', 'discord_display_name', 'linked_name', 'linked_twitter_handle', 'is_active', 'linked_at']
    list_filter = ['is_active', 'linked_at']
    search_fields = ['discord_username', 'discord_display_name', 'linked_name', 'linked_twitter_handle']
    readonly_fields = ['linked_at', 'created_at', 'updated_at']
    date_hierarchy = 'linked_at'
    fields = ['discord_user_id', 'discord_username', 'discord_display_name', 'linked_name', 'linked_twitter_handle', 'is_active', 'user', 'linked_at', 'created_at', 'updated_at']


@admin.register(TelegramUserMapping)
class TelegramUserMappingAdmin(admin.ModelAdmin):
    list_display = ['telegram_first_name', 'telegram_username', 'linked_name', 'linked_twitter_handle', 'is_active', 'linked_at']
    list_filter = ['is_active', 'linked_at']
    search_fields = ['telegram_username', 'telegram_first_name', 'telegram_last_name', 'linked_name', 'linked_twitter_handle']
    readonly_fields = ['linked_at', 'created_at', 'updated_at']
    date_hierarchy = 'linked_at'
    fields = ['telegram_user_id', 'telegram_username', 'telegram_first_name', 'telegram_last_name', 'linked_name', 'linked_twitter_handle', 'is_active', 'user', 'linked_at', 'created_at', 'updated_at']


@admin.register(Attestation)
class AttestationAdmin(admin.ModelAdmin):
    list_display = ['weekly_roll_call', 'source', 'user_display', 'posted_at', 'part_number', 'has_attachments', 'is_hidden']
    list_filter = ['weekly_roll_call', 'source', 'posted_at', 'has_attachments', 'is_hidden']
    search_fields = ['raw_text', 'discord_user__discord_username', 'discord_user__linked_name', 'telegram_user__telegram_first_name', 'telegram_user__linked_name']
    readonly_fields = ['discord_message_id', 'discord_channel_id', 'telegram_message_id', 'telegram_chat_id', 'posted_at', 'created_at', 'updated_at']
    date_hierarchy = 'posted_at'
    fields = ['weekly_roll_call', 'source', 'discord_user', 'telegram_user', 'parent_attestation', 'part_number', 'discord_message_id', 'discord_channel_id', 'telegram_message_id', 'telegram_chat_id', 'raw_text', 'has_attachments', 'attachment_count', 'is_hidden', 'hidden_at', 'parsed_data', 'posted_at', 'created_at', 'updated_at']

    def user_display(self, obj):
        if obj.source == 'discord':
            return obj.discord_user.discord_username if obj.discord_user else "Unknown"
        else:
            return obj.telegram_user.telegram_first_name if obj.telegram_user else "Unknown"
    user_display.short_description = 'User'


@admin.register(RankingTrial)
class RankingTrialAdmin(admin.ModelAdmin):
    list_display = ['weekly_roll_call', 'trial_number', 'ai_provider', 'ai_model', 'created_at', 'ranking_count']
    list_filter = ['weekly_roll_call', 'ai_provider', 'ai_model', 'created_at']
    search_fields = ['ai_model', 'weekly_roll_call__week_start_date']
    readonly_fields = ['created_at', 'parsed_rankings', 'raw_response']
    date_hierarchy = 'created_at'
    fields = ['weekly_roll_call', 'trial_number', 'ai_provider', 'ai_model', 'parsed_rankings', 'raw_response', 'created_at']

    def ranking_count(self, obj):
        """Display number of rankings in this trial"""
        if obj.parsed_rankings:
            return len(obj.parsed_rankings)
        return 0
    ranking_count.short_description = 'Rankings'


@admin.register(MessageLog)
class MessageLogAdmin(admin.ModelAdmin):
    list_display = ['source', 'user_display', 'posted_at', 'is_attestation', 'has_attachments']
    list_filter = ['source', 'is_attestation', 'posted_at']
    search_fields = ['content', 'discord_user__discord_username', 'telegram_user__telegram_first_name']
    readonly_fields = ['posted_at', 'created_at', 'updated_at']
    date_hierarchy = 'posted_at'

    def user_display(self, obj):
        if obj.source == 'discord':
            return obj.discord_user.discord_username if obj.discord_user else "Unknown"
        else:
            return obj.telegram_user.telegram_first_name if obj.telegram_user else "Unknown"
    user_display.short_description = 'User'


@admin.register(WebLoginToken)
class WebLoginTokenAdmin(admin.ModelAdmin):
    list_display = ['token', 'telegram_user', 'is_used', 'created_at', 'expires_at']
    list_filter = ['is_used', 'created_at']
    readonly_fields = ['token', 'created_at', 'confirmed_at']


@admin.register(StravaAuth)
class StravaAuthAdmin(admin.ModelAdmin):
    list_display = ['telegram_user', 'athlete_id', 'token_expires_at', 'created_at']
    list_filter = ['created_at']
    search_fields = ['telegram_user__telegram_first_name', 'athlete_id']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(StravaActivity)
class StravaActivityAdmin(admin.ModelAdmin):
    list_display = ['name', 'sport_type', 'start_date_local', 'distance_display', 'moving_time_formatted']
    list_filter = ['sport_type', 'start_date']
    search_fields = ['name', 'strava_auth__telegram_user__telegram_first_name']
    readonly_fields = ['fetched_at']
    date_hierarchy = 'start_date'

    def distance_display(self, obj):
        if obj.distance_miles:
            return f"{obj.distance_miles:.2f} mi"
        return "-"
    distance_display.short_description = 'Distance'
