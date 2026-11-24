from django.contrib import admin
from django.urls import include, path
from crm import views

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("", views.dashboard, name="dashboard"),
    path("accounts/login/", views.login_view, name="login"),
    path("accounts/logout/", views.logout_view, name="logout"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("crm.urls")),
]
