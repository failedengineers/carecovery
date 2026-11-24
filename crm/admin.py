"""Restricted Django admin: platform metadata only, never patient/clinical data."""
from django.contrib import admin
from .models import Organization, User


class SuperAdminOnlyMixin:
    def has_module_permission(self, request):
        return bool(request.user.is_active and request.user.is_superuser and request.user.role == User.Role.SUPER_ADMIN)

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Organization)
class OrganizationAdmin(SuperAdminOnlyMixin, admin.ModelAdmin):
    list_display = ("name", "account_type", "status", "email", "created_at")
    list_filter = ("account_type", "status", "created_at")
    search_fields = ("name", "email", "phone", "slug")
    readonly_fields = ("slug", "account_type", "status", "created_by", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        if not self.has_module_permission(request):
            return super().get_queryset(request).none()
        return super().get_queryset(request)


# Deliberately do not register User, Patient, Appointment, MedicalRecord,
# Prescription, Invoice, Payment, or other tenant records.
# Clinic users must use the tenant-aware application UI; the platform admin
# must not become a back door into patient records.
admin.site.site_header = "CareCovery platform administration"
admin.site.site_title = "CareCovery admin"
admin.site.index_title = "Workspace administration"
