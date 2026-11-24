"""Security middleware for private clinical workspaces."""
from django.contrib.auth import logout
from django.http import HttpResponse
from django.shortcuts import redirect

from .models import DoctorProfile, Organization, User


class SessionAccessEnforcementMiddleware:
    """Immediately revoke sessions for disabled users/workspaces/doctors.

    AuthenticationMiddleware must run before this middleware. This closes the
    gap where an already-authenticated browser session survives a staff or
    workspace suspension.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            revoke = not user.is_active
            if not revoke and user.organization_id:
                user.organization.expire_if_needed()
                revoke = user.organization.status == Organization.Status.SUSPENDED
            if not revoke and user.role == User.Role.DOCTOR:
                try:
                    profile = user.doctor_profile
                    revoke = not profile.is_active or profile.organization_id != user.organization_id
                except DoctorProfile.DoesNotExist:
                    revoke = True
            if revoke:
                logout(request)
                return redirect("login")
        return self.get_response(request)


class NoStoreForAuthenticatedMiddleware:
    """Keep authenticated clinical/financial pages out of shared browser caches."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if getattr(request, "user", None) is not None and request.user.is_authenticated:
            response["Cache-Control"] = "no-store, private, max-age=0, must-revalidate"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
        return response


class RejectFileUploadsMiddleware:
    """Reject multipart and raw binary upload requests application-wide.

    CareCovery stores structured CRM data only. This guard prevents a future
    view from accidentally accepting files or relying on Render's ephemeral
    filesystem. CSV import is deliberately implemented as pasted text.
    """

    BLOCKED_CONTENT_TYPES = {"multipart/form-data", "multipart/mixed", "application/octet-stream"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in {"POST", "PUT", "PATCH"} and (request.content_type or "").lower() in self.BLOCKED_CONTENT_TYPES:
            return HttpResponse(
                "File and media uploads are disabled in CareCovery.",
                status=415,
                content_type="text/plain; charset=utf-8",
            )
        return self.get_response(request)
