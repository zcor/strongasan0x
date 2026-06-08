from django.contrib import admin

from .models import (
    ChecklistVersion,
    CoachSuggestion,
    DailyAccessToken,
    DailyCheckIn,
    DailyCheckInAnswer,
    DailyParticipant,
)


@admin.register(DailyParticipant)
class DailyParticipantAdmin(admin.ModelAdmin):
    list_display = ("display_name", "kind", "is_active", "telegram_mapping", "created_at")
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
    list_display = ("participant", "date", "score", "source", "submitted_at")
    list_filter = ("source", "date")
    search_fields = ("participant__display_name", "comment")
    raw_id_fields = ("participant", "checklist_version")
    date_hierarchy = "date"
    inlines = [DailyCheckInAnswerInline]


@admin.register(CoachSuggestion)
class CoachSuggestionAdmin(admin.ModelAdmin):
    list_display = ("check_in", "status", "has_mutation", "model_name", "cost_usd", "created_at")
    list_filter = ("status", "model_name")
    raw_id_fields = ("check_in", "applied_version")
    readonly_fields = ("model_name", "cost_usd", "created_at", "applied_at")

    @admin.display(boolean=True, description="mutation")
    def has_mutation(self, obj):
        return bool(obj.proposed_questions)
