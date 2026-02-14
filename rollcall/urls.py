from django.urls import path, include
from . import views

urlpatterns = [
    # Warrior Dashboard (public, Telegram auth)
    path('warrior/', include('rollcall.warrior.urls')),

    path('', views.landing, name='landing'),
    path('telegram/webhook/', views.telegram_webhook, name='telegram_webhook'),
    path('account/', views.account, name='account'),
    path('account/link-discord/', views.link_discord, name='link_discord'),
    path('account/link-telegram/', views.link_telegram, name='link_telegram'),
    path('account/unlink-discord/', views.unlink_discord, name='unlink_discord'),
    path('account/unlink-telegram/', views.unlink_telegram, name='unlink_telegram'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    # Attestation review (admin only)
    path('review-attestations/', views.review_attestations, name='review_attestations'),
    path('review-attestations/toggle-hidden/<int:attestation_id>/', views.toggle_attestation_hidden, name='toggle_attestation_hidden'),
    path('review-attestations/add/', views.add_manual_attestation, name='add_manual_attestation'),
]
