from django.urls import path, include, register_converter
from . import views


class IsoDateConverter:
    """Matches a YYYY-MM-DD path segment (e.g. the roll call week_end_date)."""
    regex = r'\d{4}-\d{2}-\d{2}'

    def to_python(self, value):
        return value

    def to_url(self, value):
        return str(value)


register_converter(IsoDateConverter, 'isodate')

urlpatterns = [
    # Warrior Dashboard (public, Telegram auth)
    path('warrior/', include('rollcall.warrior.urls')),

    path('', views.landing, name='landing'),
    path('roll-call/', views.roll_call_index, name='roll_call_index'),
    path('roll-call/<isodate:week_end_date>/', views.roll_call_detail, name='roll_call_detail'),
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
