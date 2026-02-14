"""
URL configuration for strongasan0x project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
"""

from django.contrib import admin
from django.urls import path, include
from django.shortcuts import redirect

def handler404(request, exception):
    """Custom 404 handler - redirect to homepage unless path is static or admin"""
    path = request.path
    # Try to serve static files if in development mode
    if path.startswith('/static/'):
        from django.contrib.staticfiles.views import serve
        try:
            return serve(request, path.replace('/static/', ''), insecure=True)
        except:
            from django.http import HttpResponseNotFound
            return HttpResponseNotFound("Page not found")
    # Don't redirect admin - return normal 404 response
    if path.startswith('/admin/'):
        from django.http import HttpResponseNotFound
        return HttpResponseNotFound("Page not found")
    return redirect('landing')

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("rollcall.urls")),
]

# Always add staticfiles URL patterns
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
urlpatterns += staticfiles_urlpatterns()
