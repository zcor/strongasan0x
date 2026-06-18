from django.urls import path

from . import views

app_name = "daily"

urlpatterns = [
    path("checkin/", views.checkin, name="checkin"),
    path("c/<uuid:token>/", views.token_login, name="token_login"),
    path("item/", views.set_item_state, name="set_item_state"),
    path("bonus/next/", views.next_bonus, name="next_bonus"),
    path("wrap/", views.wrap_day, name="wrap_day"),
    path("comment/", views.save_comment, name="save_comment"),
    path("suggestion/<int:suggestion_id>/respond/", views.respond_to_suggestion, name="respond_to_suggestion"),
    path("baseline/", views.reset_to_baseline_view, name="reset_to_baseline"),
    # PWA: installable home-screen app (manifest + service worker).
    path("manifest.webmanifest", views.manifest, name="manifest"),
    path("sw.js", views.service_worker, name="service_worker"),
]
