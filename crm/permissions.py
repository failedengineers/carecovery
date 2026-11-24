from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from .models import User, Organization, DoctorProfile, Patient, Appointment, MedicalRecord, FollowUp, Invoice


def role_required(*roles):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect("login")
            if request.user.role not in roles:
                raise PermissionDenied("You do not have permission to access this area.")
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


def organization_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if not request.user.organization_id:
            messages.error(request, "This account is not linked to a clinic workspace.")
            return redirect("dashboard")
        request.user.organization.expire_if_needed()
        if request.user.organization.status == Organization.Status.SUSPENDED:
            raise PermissionDenied("This workspace is suspended or its trial/license has expired. Contact CareCovery support.")
        return view_func(request, *args, **kwargs)
    return _wrapped


def doctor_profile_for(user):
    try:
        profile = user.doctor_profile
        if profile.organization_id != user.organization_id or not profile.is_active:
            return None
        return profile
    except DoctorProfile.DoesNotExist:
        return None


def visible_patients(user):
    if not user.organization_id:
        return Patient.objects.none()
    qs = Patient.objects.filter(organization_id=user.organization_id, is_active=True)
    if user.role == User.Role.DOCTOR:
        profile = doctor_profile_for(user)
        if not profile:
            return qs.none()
        qs = qs.filter(Q(primary_doctor=profile) | Q(doctors=profile)).distinct()
    return qs


def visible_appointments(user):
    if not user.organization_id:
        return Appointment.objects.none()
    qs = Appointment.objects.filter(organization_id=user.organization_id).select_related("patient", "doctor__user")
    if user.role == User.Role.DOCTOR:
        profile = doctor_profile_for(user)
        if not profile:
            return qs.none()
        qs = qs.filter(doctor=profile)
    return qs


def visible_records(user):
    if not user.organization_id:
        return MedicalRecord.objects.none()
    qs = MedicalRecord.objects.filter(organization_id=user.organization_id).select_related("patient", "doctor__user")
    if user.role == User.Role.DOCTOR:
        profile = doctor_profile_for(user)
        if not profile:
            return qs.none()
        qs = qs.filter(doctor=profile)
    elif user.role == User.Role.NURSE:
        qs = qs.filter(is_sensitive=False)
    elif user.role == User.Role.BILLING:
        return qs.none()
    return qs


def visible_followups(user):
    if not user.organization_id:
        return FollowUp.objects.none()
    qs = FollowUp.objects.filter(organization_id=user.organization_id).select_related("patient", "doctor__user")
    if user.role == User.Role.DOCTOR:
        profile = doctor_profile_for(user)
        return qs.filter(doctor=profile) if profile else qs.none()
    if user.role == User.Role.BILLING:
        return qs.none()
    return qs


def visible_invoices(user):
    if not user.organization_id:
        return Invoice.objects.none()
    qs = Invoice.objects.filter(organization_id=user.organization_id).select_related("patient", "doctor__user")
    if user.role == User.Role.DOCTOR:
        profile = doctor_profile_for(user)
        return qs.filter(doctor=profile) if profile else qs.none()
    if user.role == User.Role.NURSE:
        return qs.none()
    return qs

