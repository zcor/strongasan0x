from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('api/messages/', views.api_messages, name='api_messages'),
    path('api/stats/', views.api_stats, name='api_stats'),
]





