from django.urls import path

from . import views

app_name = "daily"

urlpatterns = [
    path("checkin/", views.checkin, name="checkin"),
    path("c/<uuid:token>/", views.token_login, name="token_login"),
    path("item/", views.set_item_state, name="set_item_state"),
    path("item/add/", views.add_item, name="add_item"),
    path("item/edit/", views.edit_item, name="edit_item"),
    path("item/remove/", views.remove_item, name="remove_item"),
    path("habits/", views.habits_edit, name="habits_edit"),
    # Nested detail checklists: add/remove a sub-item under a core habit.
    path("subitem/add/", views.add_subitem, name="add_subitem"),
    path("subitem/remove/", views.remove_subitem, name="remove_subitem"),
    # Wins backlog (beta): add to the pile, act on the surfaced "today's win".
    path("win/add/", views.win_add, name="win_add"),
    path("win/", views.win_action, name="win_action"),
    path("win/select/", views.win_select, name="win_select"),
    path("win/candidates/", views.win_candidates, name="win_candidates"),
    # Wins editor ("your list" door): north stars with stepping stones + one-offs.
    path("wins/", views.wins_edit, name="wins_edit"),
    # Finished north stars with their full step ladders (read-only).
    path("wins/achieved/", views.wins_achieved, name="wins_achieved"),
    path("wins/archived/", views.wins_archived, name="wins_archived"),
    path("win/goal/add/", views.win_goal_add, name="win_goal_add"),
    path("win/goal/edit/", views.win_goal_edit, name="win_goal_edit"),
    path("win/goal/complete/", views.win_goal_complete, name="win_goal_complete"),
    path("win/goal/archive/", views.win_goal_archive, name="win_goal_archive"),
    path("win/goal/restore/", views.win_goal_restore, name="win_goal_restore"),
    path("win/stone/add/", views.win_stone_add, name="win_stone_add"),
    path("win/remove/", views.win_remove, name="win_remove"),
    path("bonus/next/", views.next_bonus, name="next_bonus"),
    path("wrap/", views.wrap_day, name="wrap_day"),
    path("comment/", views.save_comment, name="save_comment"),
    path("suggestion/<int:suggestion_id>/respond/", views.respond_to_suggestion, name="respond_to_suggestion"),
    path("baseline/", views.reset_to_baseline_view, name="reset_to_baseline"),
    # PWA: installable home-screen app (manifest + service worker).
    path("manifest.webmanifest", views.manifest, name="manifest"),
    path("sw.js", views.service_worker, name="service_worker"),
    # Web Push: device registers here so the morning job can badge it.
    path("push/subscribe/", views.push_subscribe, name="push_subscribe"),
    # Per-user timezone capture: the browser POSTs its IANA tz on app load so
    # the day boundary (today/streak/badge) is the user's local day.
    path("timezone/", views.set_timezone, name="set_timezone"),
    # Naked-user onboarding: a few multiple-choice taps → seeded checklist.
    path("onboarding/", views.submit_onboarding, name="submit_onboarding"),
    path("onboarding/beta/", views.submit_onboarding_beta, name="submit_onboarding_beta"),
    # Metrics (Spencer-persona): log a number reading.
    path("metric/", views.save_metric, name="save_metric"),
    # Coach chat: history is lazy-loaded when the beta sheet first opens;
    # messages remain the live two-way successor to the comment box.
    path("morning-report/", views.morning_report, name="morning_report"),
    path("chat/history/", views.chat_history, name="chat_history"),
    path("chat/", views.chat_send, name="chat_send"),
]
