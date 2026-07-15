from django.contrib import admin

from .models import (
    ChecklistVersion,
    CoachProfile,
    CoachSuggestion,
    DailyAccessToken,
    DailyCheckIn,
    DailyCheckInAnswer,
    DailyParticipant,
)


@admin.register(DailyParticipant)
class DailyParticipantAdmin(admin.ModelAdmin):
    list_display = ("display_name", "kind", "is_active", "morning_target_hour",
                    "evening_nudge_hour", "telegram_mapping", "created_at")
    list_filter = ("kind", "is_active")
    search_fields = ("display_name",)
    raw_id_fields = ("telegram_mapping",)


@admin.register(DailyAccessToken)
class DailyAccessTokenAdmin(admin.ModelAdmin):
    list_display = ("participant", "token", "created_at", "last_used_at", "revoked_at")
    list_filter = ("revoked_at",)
    search_fields = ("participant__display_name", "token")
    raw_id_fields = ("participant",)
    readonly_fields = ("token", "created_at", "last_used_at")


@admin.register(ChecklistVersion)
class ChecklistVersionAdmin(admin.ModelAdmin):
    list_display = ("participant", "id", "source", "is_current", "created_at")
    list_filter = ("source", "is_current")
    raw_id_fields = ("participant", "derived_from")
    readonly_fields = ("created_at",)


class DailyCheckInAnswerInline(admin.TabularInline):
    model = DailyCheckInAnswer
    extra = 0


@admin.register(DailyCheckIn)
class DailyCheckInAdmin(admin.ModelAdmin):
    list_display = ("participant", "date", "score", "done_count", "source", "submitted_at")
    list_filter = ("source", "date")
    search_fields = ("participant__display_name", "comment")
    raw_id_fields = ("participant", "checklist_version")
    date_hierarchy = "date"
    inlines = [DailyCheckInAnswerInline]

    def save_formset(self, request, form, formset, change):
        """Keep derived streak state correct when answers are edited in admin."""
        super().save_formset(request, form, formset, change)
        if formset.model is DailyCheckInAnswer:
            from .services.streaks import refresh_streak_cache

            check_in = form.instance
            refresh_streak_cache(
                check_in.participant,
                changed_check_in=check_in,
            )


@admin.register(CoachSuggestion)
class CoachSuggestionAdmin(admin.ModelAdmin):
    list_display = ("check_in", "status", "has_mutation", "model_name", "cost_usd", "created_at")
    list_filter = ("status", "model_name")
    raw_id_fields = ("check_in", "applied_version")
    readonly_fields = ("model_name", "cost_usd", "created_at", "applied_at")

    @admin.display(boolean=True, description="mutation")
    def has_mutation(self, obj):
        return bool(obj.proposed_questions)


@admin.register(CoachProfile)
class CoachProfileAdmin(admin.ModelAdmin):
    list_display = ("participant", "attestation_count", "model_name", "cost_usd", "updated_at")
    search_fields = ("participant__display_name",)
    raw_id_fields = ("participant",)
    readonly_fields = ("source_hash", "attestation_count", "model_name", "cost_usd", "created_at", "updated_at")
