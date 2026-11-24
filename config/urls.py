from django.contrib import admin
from django.urls import include, path
from crm import views

urlpatterns = [
    path("django-admin/", admin.site.urls),

    # Public landing page
    path("", views.landing, name="landing"),

    # Dashboard
    path("dashboard/", views.dashboard, name="dashboard"),

    # Authentication
    path("accounts/login/", views.login_view, name="login"),
    path("accounts/logout/", views.logout_view, name="logout"),
    path("accounts/", include("django.contrib.auth.urls")),

    # All CRM URLs
    path("", include("crm.urls")),
]