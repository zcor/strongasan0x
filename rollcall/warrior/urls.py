"""URL routing for the Warrior Dashboard."""
from django.urls import path
from . import views

app_name = 'warrior'

urlpatterns = [
    path('', views.warrior_index, name='index'),
    path('login/', views.warrior_login, name='login'),
    path('check-login/', views.check_login, name='check_login'),
    path('callback/', views.telegram_callback, name='telegram_callback'),
    path('logout/', views.warrior_logout, name='logout'),
    path('dashboard/', views.warrior_dashboard, name='dashboard'),
    path('history/', views.attestation_history, name='history'),
    path('edit/', views.edit_attestation, name='edit'),
    # Strava integration
    path('strava/link/', views.link_strava, name='link_strava'),
    path('strava/callback/', views.strava_callback, name='strava_callback'),
    path('strava/unlink/', views.unlink_strava, name='unlink_strava'),
    path('strava/attestation/', views.strava_attestation, name='strava_attestation'),
]
