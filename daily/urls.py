from django.urls import path

from . import views

app_name = "daily"

urlpatterns = [
    path("checkin/", views.checkin, name="checkin"),
    path("c/<uuid:token>/", views.token_login, name="token_login"),
    path("tomorrow/preview.json", views.tomorrow_preview_json, name="tomorrow_preview_json"),
    path("tomorrow/modify/", views.modify_tomorrow, name="modify_tomorrow"),
    path("tomorrow/reject/", views.reject_tomorrow, name="reject_tomorrow"),
    path("suggestion/<int:suggestion_id>/respond/", views.respond_to_suggestion, name="respond_to_suggestion"),
    path("baseline/", views.reset_to_baseline_view, name="reset_to_baseline"),
]
