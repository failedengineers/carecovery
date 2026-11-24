from datetime import timedelta
from decimal import Decimal
import csv
import io
import secrets
import logging
from datetime import date

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, Sum, Count, Case, When, Value, IntegerField, F
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.cache import never_cache
from django.utils.http import url_has_allowed_host_and_scheme

from .forms import (
    PatientForm, AppointmentForm, MedicalRecordForm, FollowUpForm,
    InvoiceForm, PaymentForm, OrganizationForm, StaffUserForm,
    PracticeExpenseForm, InventoryItemForm, InventoryMovementForm, PatientImportForm,
)
from .models import (
    User, Organization, DoctorProfile, Patient, PatientAccess, Appointment,
    MedicalRecord, Prescription, PrescriptionItem, Invoice, Payment,
    FollowUp, AuditLog, Notification, QueueEntry, PracticeExpense, InventoryItem, InventoryMovement,
)
from .permissions import (
    role_required, organization_required, visible_patients,
    visible_appointments, visible_records, visible_followups,
    visible_invoices, doctor_profile_for,
)


PLATFORM_ROLES = [User.Role.SUPER_ADMIN, User.Role.ONBOARDING_AGENT]
CLINICAL_ROLES = [User.Role.CLINIC_MANAGER, User.Role.DOCTOR]
logger = logging.getLogger("crm")


def _paginate(request, queryset, per_page=20):
    page_obj = Paginator(queryset, per_page).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return page_obj, params.urlencode()


def _appointment_conflict(doctor, starts_at, duration_minutes, exclude_pk=None):
    """Recheck overlap inside a transaction after locking the doctor row."""
    candidate_end = starts_at + timedelta(minutes=duration_minutes)
    qs = Appointment.objects.filter(
        organization_id=doctor.organization_id,
        doctor=doctor,
        starts_at__lt=candidate_end,
        status__in=[Appointment.Status.REQUESTED, Appointment.Status.CONFIRMED, Appointment.Status.CHECKED_IN, Appointment.Status.IN_PROGRESS],
    )
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    for existing in qs.only("pk", "starts_at", "duration_minutes"):
        if existing.ends_at > starts_at:
            return existing
    return None


def _audit(request, action, obj=None, description=""):
    organization = getattr(obj, "organization", None) if obj is not None else None
    if organization is None:
        organization = getattr(request.user, "organization", None)
    AuditLog.objects.create(
        organization=organization,
        actor=request.user if request.user.is_authenticated else None,
        action=action,
        object_type=obj.__class__.__name__ if obj else "",
        object_id=str(obj.pk) if obj else "",
        description=description,
        ip_address=request.META.get("REMOTE_ADDR"),
    )


def _notify(user, title, message, url=""):
    if user:
        Notification.objects.create(user=user, title=title, message=message, url=url)


def _send_account_setup_email_after_commit(request, user):
    """Send a one-time password setup link without exposing a temporary password."""
    user_id = user.pk

    def send_setup_email():
        try:
            invited_user = User.objects.get(pk=user_id, is_active=True)
            form = PasswordResetForm({"email": invited_user.email})
            if form.is_valid():
                form.save(
                    request=request,
                    use_https=request.is_secure(),
                    email_template_name="registration/password_reset_email.html",
                    subject_template_name="registration/password_reset_subject.txt",
                )
        except Exception:
            # Keep the account creation transaction committed; the user can use
            # the Forgot password screen to request another setup link.
            logger.exception("Account setup email could not be sent for user id %s", user_id)

    transaction.on_commit(send_setup_email)


def _restrict_patient_form_for_reception(form, user):
    """Clinic Manager combines clinic administration and reception permissions."""
    return form


@never_cache
def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None and user.is_active:
            if user.organization_id:
                user.organization.expire_if_needed()
            if user.organization_id and user.organization.status == Organization.Status.SUSPENDED:
                reason = user.organization.suspension_reason or "This workspace is currently suspended."
                messages.error(request, f"{reason}. Please contact CareCovery support or your account administrator.")
            elif user.role == User.Role.DOCTOR and not DoctorProfile.objects.filter(
                user=user, organization_id=user.organization_id, is_active=True
            ).exists():
                messages.error(request, "Your doctor workspace is not active. Please contact your clinic administrator.")
            else:
                login(request, user)
                return redirect("dashboard")
        else:
            messages.error(request, "The username or password was incorrect.")
    return render(request, "crm/login.html")


@require_POST
@login_required
def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
def dashboard(request):
    user = request.user
    if user.role in PLATFORM_ROLES:
        Organization.expire_overdue()
        orgs = Organization.objects.all()
        if user.role == User.Role.ONBOARDING_AGENT:
            orgs = orgs.filter(created_by=user)
        context = {
            "platform_view": True,
            "org_count": orgs.count(),
            "active_org_count": orgs.filter(status=Organization.Status.ACTIVE).count(),
            "doctor_count": DoctorProfile.objects.filter(organization__in=orgs).count(),
            "patient_count": Patient.objects.filter(organization__in=orgs).count(),
            "recent_orgs": orgs.order_by("-created_at")[:6],
            "recent_audit": AuditLog.objects.filter(organization__in=orgs)[:8],
        }
        return render(request, "crm/dashboard.html", context)

    if not user.organization_id:
        return render(request, "crm/no_workspace.html")

    today = timezone.localdate()
    patients = visible_patients(user)
    appointments = visible_appointments(user)
    records = visible_records(user)
    today_appointments = appointments.filter(starts_at__date=today).exclude(status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW])
    upcoming = appointments.filter(starts_at__gte=timezone.now()).exclude(status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW])[:6]
    open_followups = visible_followups(user).filter(status=FollowUp.Status.OPEN, due_date__lte=today + timedelta(days=7))
    invoices = visible_invoices(user)
    overdue = open_followups.filter(due_date__lt=today).count()
    waiting_queue = QueueEntry.objects.filter(organization=user.organization, queue_date=today, status=QueueEntry.Status.WAITING)
    can_view_expenses = user.role in {User.Role.CLINIC_MANAGER, User.Role.BILLING} or (user.role == User.Role.DOCTOR and user.organization.account_type == Organization.AccountType.INDIVIDUAL)
    expenses_month_total = PracticeExpense.objects.filter(organization=user.organization, expense_date__year=today.year, expense_date__month=today.month).aggregate(total=Sum("amount"))["total"] or Decimal("0") if can_view_expenses else Decimal("0")
    context = {
        "platform_view": False,
        "patient_count": patients.count(),
        "today_count": today_appointments.count(),
        "record_count": records.count(),
        "followup_count": open_followups.count(),
        "overdue_followup_count": overdue,
        "waiting_count": waiting_queue.count(),
        "upcoming_appointments": upcoming,
        "today_appointments": today_appointments.select_related("patient", "doctor__user")[:8],
        "recent_patients": patients.order_by("-created_at")[:6],
        "pending_amount": invoices.exclude(status__in=[Invoice.Status.PAID, Invoice.Status.VOID]).aggregate(total=Sum("total") - Sum("amount_paid"))["total"] or Decimal("0"),
        "expenses_month": expenses_month_total,
        "low_stock_count": InventoryItem.objects.filter(organization=user.organization, is_active=True, quantity__lte=F("reorder_level")).count(),
        "is_doctor": user.role == User.Role.DOCTOR,
    }
    return render(request, "crm/dashboard.html", context)


@login_required
@organization_required
def patient_list(request):
    qs = visible_patients(request.user).select_related("primary_doctor__user")
    q = request.GET.get("q", "").strip()
    doctor = request.GET.get("doctor", "")
    blood = request.GET.get("blood", "")
    tag = request.GET.get("tag", "").strip()
    gender = request.GET.get("gender", "")
    if q:
        qs = qs.filter(Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(phone__icontains=q) | Q(mrn__icontains=q) | Q(email__icontains=q))
    if doctor and request.user.role != User.Role.DOCTOR:
        qs = qs.filter(primary_doctor_id=doctor)
    if blood and request.user.role != User.Role.BILLING:
        qs = qs.filter(blood_group=blood)
    if gender:
        qs = qs.filter(gender=gender)
    if tag:
        qs = qs.filter(tags__icontains=tag)
    doctors = DoctorProfile.objects.filter(organization=request.user.organization, is_active=True).select_related("user")
    page_obj, querystring = _paginate(request, qs.order_by("-created_at"))
    return render(request, "crm/patients.html", {"patients": page_obj.object_list, "page_obj": page_obj, "querystring": querystring, "doctors": doctors, "filters": request.GET})


@login_required
@organization_required
def patient_create(request):
    if request.user.role not in {User.Role.CLINIC_MANAGER, User.Role.DOCTOR}:
        raise PermissionDenied
    form = _restrict_patient_form_for_reception(PatientForm(request.POST or None), request.user)
    form.fields["primary_doctor"].queryset = DoctorProfile.objects.filter(organization=request.user.organization, is_active=True)
    if request.user.role == User.Role.DOCTOR:
        profile = doctor_profile_for(request.user)
        if not profile:
            raise PermissionDenied("Doctor profile is not configured.")
        form.fields["primary_doctor"].queryset = DoctorProfile.objects.filter(pk=profile.pk)
        form.fields["primary_doctor"].initial = profile
    if request.method == "POST" and form.is_valid():
        patient = form.save(commit=False)
        patient.organization = request.user.organization
        patient.created_by = request.user
        if request.user.role == User.Role.DOCTOR:
            patient.primary_doctor = doctor_profile_for(request.user)
        patient.save()
        if patient.primary_doctor:
            PatientAccess.objects.get_or_create(patient=patient, doctor=patient.primary_doctor, defaults={"granted_by": request.user, "reason": "Primary doctor"})
        _audit(request, "patient.created", patient, "Created patient profile")
        messages.success(request, "Patient profile created.")
        return redirect("patient_detail", pk=patient.pk)
    return render(request, "crm/form_page.html", {"form": form, "title": "Add patient", "subtitle": "Create a secure patient profile", "cancel_url": "patients", "submit_label": "Save patient"})


@login_required
@organization_required
def patient_edit(request, pk):
    if request.user.role not in {User.Role.CLINIC_MANAGER, User.Role.DOCTOR}:
        raise PermissionDenied
    patient = get_object_or_404(visible_patients(request.user), pk=pk)
    form = _restrict_patient_form_for_reception(PatientForm(request.POST or None, instance=patient), request.user)
    form.fields["primary_doctor"].queryset = DoctorProfile.objects.filter(organization=request.user.organization, is_active=True)
    if request.user.role == User.Role.DOCTOR:
        profile = doctor_profile_for(request.user)
        form.fields["primary_doctor"].queryset = DoctorProfile.objects.filter(pk=profile.pk) if profile else DoctorProfile.objects.none()
    if request.method == "POST" and form.is_valid():
        previous_primary_doctor_id = patient.primary_doctor_id
        updated = form.save(commit=False)
        if request.user.role == User.Role.DOCTOR:
            updated.primary_doctor = doctor_profile_for(request.user)
        updated.save()
        if previous_primary_doctor_id and previous_primary_doctor_id != updated.primary_doctor_id:
            PatientAccess.objects.filter(patient=updated, doctor_id=previous_primary_doctor_id, reason="Primary doctor").delete()
        if updated.primary_doctor:
            PatientAccess.objects.get_or_create(patient=updated, doctor=updated.primary_doctor, defaults={"granted_by": request.user, "reason": "Primary doctor"})
        _audit(request, "patient.updated", updated, "Updated patient profile")
        messages.success(request, "Patient profile updated.")
        return redirect("patient_detail", pk=updated.pk)
    return render(request, "crm/form_page.html", {"form": form, "title": "Edit patient", "subtitle": "Update the patient profile and contact details", "cancel_url": "patient_detail", "cancel_pk": patient.pk, "submit_label": "Save patient"})


@login_required
@organization_required
def patient_detail(request, pk):
    patient = get_object_or_404(visible_patients(request.user), pk=pk)
    _audit(request, "patient.viewed", patient, "Opened patient record")
    appointments = visible_appointments(request.user).filter(patient=patient).order_by("-starts_at")[:8]
    records = visible_records(request.user).filter(patient=patient).order_by("-visit_date")[:8]
    invoices = visible_invoices(request.user).filter(patient=patient).order_by("-issue_date")[:5]
    followups = visible_followups(request.user).filter(patient=patient).order_by("due_date")[:5]
    can_view_clinical_snapshot = request.user.role != User.Role.BILLING
    can_view_appointment_reason = request.user.role != User.Role.BILLING
    can_view_followup_title = request.user.role != User.Role.BILLING
    return render(request, "crm/patient_detail.html", {
        "patient": patient, "appointments": appointments, "records": records,
        "invoices": invoices, "followups": followups,
        "can_view_clinical_snapshot": can_view_clinical_snapshot,
        "can_view_appointment_reason": can_view_appointment_reason,
        "can_view_followup_title": can_view_followup_title,
    })


@login_required
@organization_required
def appointment_list(request):
    qs = visible_appointments(request.user)
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    date_filter = request.GET.get("date", "")
    doctor = request.GET.get("doctor", "")
    kind = request.GET.get("kind", "")
    if q:
        patient_match = Q(patient__first_name__icontains=q) | Q(patient__last_name__icontains=q) | Q(patient__mrn__icontains=q)
        if request.user.role != User.Role.BILLING:
            patient_match |= Q(reason__icontains=q)
        qs = qs.filter(patient_match)
    if status:
        qs = qs.filter(status=status)
    if date_filter:
        try:
            qs = qs.filter(starts_at__date=date.fromisoformat(date_filter))
        except ValueError:
            messages.error(request, "Enter a valid appointment date.")
    if doctor and request.user.role != User.Role.DOCTOR:
        qs = qs.filter(doctor_id=doctor)
    if kind:
        qs = qs.filter(kind=kind)
    doctors = DoctorProfile.objects.filter(organization=request.user.organization, is_active=True).select_related("user")
    page_obj, querystring = _paginate(request, qs.order_by("starts_at"))
    return render(request, "crm/appointments.html", {"appointments": page_obj.object_list, "page_obj": page_obj, "querystring": querystring, "doctors": doctors, "filters": request.GET, "statuses": Appointment.Status.choices, "kinds": Appointment.Kind.choices, "today": timezone.localdate()})


@login_required
@organization_required
def appointment_create(request):
    if request.user.role not in {User.Role.CLINIC_MANAGER, User.Role.DOCTOR}:
        raise PermissionDenied
    form = AppointmentForm(request.POST or None)
    form.fields["patient"].queryset = visible_patients(request.user)
    form.fields["doctor"].queryset = DoctorProfile.objects.filter(organization=request.user.organization, is_active=True).select_related("user")
    if request.user.role == User.Role.DOCTOR:
        profile = doctor_profile_for(request.user)
        form.fields["doctor"].queryset = DoctorProfile.objects.filter(pk=profile.pk) if profile else DoctorProfile.objects.none()
    if request.method == "POST" and form.is_valid():
        appointment = form.save(commit=False)
        appointment.organization = request.user.organization
        appointment.created_by = request.user
        if request.user.role == User.Role.DOCTOR:
            appointment.doctor = doctor_profile_for(request.user)
        if appointment.patient.organization_id != request.user.organization_id or not appointment.doctor or appointment.doctor.organization_id != request.user.organization_id:
            raise PermissionDenied
        with transaction.atomic():
            locked_doctor = DoctorProfile.objects.select_for_update().get(pk=appointment.doctor_id, organization=request.user.organization)
            clash = _appointment_conflict(locked_doctor, appointment.starts_at, appointment.duration_minutes)
            if clash:
                form.add_error("starts_at", f"This doctor already has an appointment from {timezone.localtime(clash.starts_at):%I:%M %p} to {timezone.localtime(clash.ends_at):%I:%M %p}.")
                return render(request, "crm/form_page.html", {"form": form, "title": "Schedule appointment", "subtitle": "Book a visit with the right doctor", "cancel_url": "appointments", "submit_label": "Schedule appointment"})
            appointment.doctor = locked_doctor
            appointment.save()
            PatientAccess.objects.get_or_create(patient=appointment.patient, doctor=appointment.doctor, defaults={"granted_by": request.user, "reason": "Appointment assignment"})
            if appointment.doctor.user_id != request.user.id:
                _notify(appointment.doctor.user, "New appointment assigned", f"{appointment.patient.full_name} · {timezone.localtime(appointment.starts_at):%d %b, %I:%M %p}", "/appointments/")
            _audit(request, "appointment.created", appointment, "Created appointment")
        messages.success(request, "Appointment scheduled. The doctor's patient access has been updated.")
        return redirect("appointments")
    return render(request, "crm/form_page.html", {"form": form, "title": "Schedule appointment", "subtitle": "Book a visit with the right doctor", "cancel_url": "appointments", "submit_label": "Schedule appointment"})


@login_required
@organization_required
def appointment_edit(request, pk):
    if request.user.role in [User.Role.NURSE, User.Role.BILLING]:
        raise PermissionDenied
    appointment = get_object_or_404(visible_appointments(request.user), pk=pk)
    if appointment.status in [Appointment.Status.COMPLETED, Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW]:
        messages.error(request, "Completed or closed appointments cannot be rescheduled.")
        return redirect("appointments")
    form = AppointmentForm(request.POST or None, instance=appointment)
    form.fields["patient"].queryset = visible_patients(request.user)
    form.fields["doctor"].queryset = DoctorProfile.objects.filter(organization=request.user.organization, is_active=True)
    if request.user.role == User.Role.DOCTOR:
        profile = doctor_profile_for(request.user)
        form.fields["doctor"].queryset = DoctorProfile.objects.filter(pk=profile.pk) if profile else DoctorProfile.objects.none()
    if request.method == "POST" and form.is_valid():
        updated = form.save(commit=False)
        updated.organization = request.user.organization
        if request.user.role == User.Role.DOCTOR:
            updated.doctor = doctor_profile_for(request.user)
        if updated.patient.organization_id != request.user.organization_id or not updated.doctor or updated.doctor.organization_id != request.user.organization_id:
            raise PermissionDenied
        with transaction.atomic():
            doctor_ids = sorted({appointment.doctor_id, updated.doctor_id})
            list(DoctorProfile.objects.select_for_update().filter(pk__in=doctor_ids, organization=request.user.organization).order_by("pk"))
            locked_doctor = DoctorProfile.objects.get(pk=updated.doctor_id, organization=request.user.organization)
            clash = _appointment_conflict(locked_doctor, updated.starts_at, updated.duration_minutes, exclude_pk=appointment.pk)
            if clash:
                form.add_error("starts_at", f"This doctor already has an appointment from {timezone.localtime(clash.starts_at):%I:%M %p} to {timezone.localtime(clash.ends_at):%I:%M %p}.")
                return render(request, "crm/form_page.html", {"form": form, "title": "Reschedule appointment", "subtitle": "Update the visit time, doctor or appointment details", "cancel_url": "appointments", "submit_label": "Save appointment"})
            old_patient_id = appointment.patient_id
            old_doctor_id = appointment.doctor_id
            updated.doctor = locked_doctor
            updated.save()
            PatientAccess.objects.get_or_create(patient=updated.patient, doctor=updated.doctor, defaults={"granted_by": request.user, "reason": "Appointment assignment"})
            if old_patient_id != updated.patient_id or old_doctor_id != updated.doctor_id:
                old_patient = Patient.objects.get(pk=old_patient_id, organization=request.user.organization)
                still_assigned = Appointment.objects.filter(
                    patient_id=old_patient_id, doctor_id=old_doctor_id,
                    status__in=[Appointment.Status.REQUESTED, Appointment.Status.CONFIRMED, Appointment.Status.CHECKED_IN, Appointment.Status.IN_PROGRESS],
                ).exists()
                if not still_assigned and old_patient.primary_doctor_id != old_doctor_id:
                    PatientAccess.objects.filter(
                        patient_id=old_patient_id, doctor_id=old_doctor_id, reason="Appointment assignment"
                    ).delete()
            _audit(request, "appointment.updated", updated, "Updated appointment schedule")
        messages.success(request, "Appointment updated.")
        return redirect("appointments")
    return render(request, "crm/form_page.html", {"form": form, "title": "Reschedule appointment", "subtitle": "Update the visit time, doctor or appointment details", "cancel_url": "appointments", "submit_label": "Save appointment"})


@login_required
@organization_required
@require_POST
def appointment_status(request, pk):
    if request.user.role == User.Role.BILLING:
        raise PermissionDenied
    appointment = get_object_or_404(visible_appointments(request.user), pk=pk)
    new_status = request.POST.get("status")
    allowed_transitions = {
        Appointment.Status.REQUESTED: {Appointment.Status.CONFIRMED, Appointment.Status.CHECKED_IN, Appointment.Status.CANCELLED},
        Appointment.Status.CONFIRMED: {Appointment.Status.CHECKED_IN, Appointment.Status.IN_PROGRESS, Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW},
        Appointment.Status.CHECKED_IN: {Appointment.Status.IN_PROGRESS, Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW},
        Appointment.Status.IN_PROGRESS: {Appointment.Status.COMPLETED},
        Appointment.Status.COMPLETED: set(),
        Appointment.Status.CANCELLED: set(),
        Appointment.Status.NO_SHOW: set(),
    }
    if new_status not in allowed_transitions.get(appointment.status, set()):
        raise PermissionDenied("That appointment status transition is not allowed.")
    if request.user.role == User.Role.NURSE and new_status not in [Appointment.Status.CHECKED_IN, Appointment.Status.IN_PROGRESS, Appointment.Status.COMPLETED]:
        raise PermissionDenied
    old = appointment.status
    appointment.status = new_status
    appointment.save(update_fields=["status"])
    _audit(request, "appointment.status_changed", appointment, f"Status changed from {old} to {new_status}")
    messages.success(request, "Appointment status updated.")
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(next_url)
    return redirect("appointments")


@login_required
@organization_required
def medical_record_list(request):
    if request.user.role == User.Role.BILLING:
        raise PermissionDenied
    qs = visible_records(request.user)
    q = request.GET.get("q", "").strip()
    doctor = request.GET.get("doctor", "")
    if q:
        qs = qs.filter(Q(patient__first_name__icontains=q) | Q(patient__last_name__icontains=q) | Q(diagnosis__icontains=q) | Q(patient__mrn__icontains=q))
    if doctor and request.user.role != User.Role.DOCTOR:
        qs = qs.filter(doctor_id=doctor)
    doctors = DoctorProfile.objects.filter(organization=request.user.organization, is_active=True).select_related("user")
    page_obj, querystring = _paginate(request, qs)
    return render(request, "crm/records.html", {"records": page_obj.object_list, "page_obj": page_obj, "querystring": querystring, "doctors": doctors, "filters": request.GET})


@login_required
@organization_required
def medical_record_create(request):
    if request.user.role not in CLINICAL_ROLES:
        raise PermissionDenied
    form = MedicalRecordForm(request.POST or None)
    form.fields["patient"].queryset = visible_patients(request.user)
    form.fields["appointment"].queryset = visible_appointments(request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            record = form.save(commit=False)
            record.organization = request.user.organization
            if request.user.role == User.Role.DOCTOR:
                record.doctor = doctor_profile_for(request.user)
            elif record.appointment:
                record.doctor = record.appointment.doctor
            else:
                if not record.patient.primary_doctor:
                    form.add_error("patient", "Assign a primary doctor before adding this clinical note.")
                    return render(request, "crm/form_page.html", {"form": form, "title": "New clinical note", "subtitle": "Document the patient visit", "cancel_url": "records"})
                record.doctor = record.patient.primary_doctor
            if record.patient.organization_id != request.user.organization_id or record.doctor.organization_id != request.user.organization_id:
                raise PermissionDenied
            record.save()
            raw_lines = form.cleaned_data.get("prescription_lines", "")
            parsed_items = []
            for line in raw_lines.splitlines():
                parts = [part.strip() for part in line.split("|")]
                if parts and parts[0]:
                    parsed_items.append(parts)
            if parsed_items:
                prescription = Prescription.objects.create(record=record)
                for parts in parsed_items:
                    parts += [""] * (5 - len(parts))
                    PrescriptionItem.objects.create(
                        prescription=prescription,
                        medicine=parts[0][:180], dosage=parts[1][:100], frequency=parts[2][:100],
                        duration=parts[3][:100], instructions=parts[4][:255],
                    )
            if record.follow_up_date:
                followup = FollowUp.objects.create(
                    organization=record.organization, patient=record.patient, doctor=record.doctor,
                    title="Clinical follow-up", due_date=record.follow_up_date,
                    generated_from_record=record, created_by=request.user,
                )
                _notify(record.doctor.user, "Follow-up added", f"{record.patient.full_name} · due {followup.due_date:%d %b %Y}", "/follow-ups/")
            _audit(request, "medical_record.created", record, "Created clinical record")
        messages.success(request, "Clinical note saved to the patient's timeline.")
        return redirect("patient_detail", pk=record.patient_id)
    return render(request, "crm/form_page.html", {"form": form, "title": "New clinical note", "subtitle": "Document the patient visit and optional prescription", "cancel_url": "records", "submit_label": "Save clinical note"})


@login_required
@organization_required
def followup_list(request):
    qs = visible_followups(request.user)
    status = request.GET.get("status", "open")
    q = request.GET.get("q", "").strip()
    priority = request.GET.get("priority", "")
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(patient__first_name__icontains=q) | Q(patient__last_name__icontains=q) | Q(title__icontains=q) | Q(patient__mrn__icontains=q))
    if priority:
        qs = qs.filter(priority=priority)
    page_obj, querystring = _paginate(request, qs.order_by("due_date"))
    return render(request, "crm/followups.html", {"followups": page_obj.object_list, "page_obj": page_obj, "querystring": querystring, "filters": request.GET, "today": timezone.localdate()})


@login_required
@organization_required
def followup_create(request):
    if request.user.role == User.Role.BILLING:
        raise PermissionDenied
    form = FollowUpForm(request.POST or None)
    form.fields["patient"].queryset = visible_patients(request.user)
    form.fields["doctor"].queryset = DoctorProfile.objects.filter(organization=request.user.organization, is_active=True)
    if request.user.role == User.Role.DOCTOR:
        profile = doctor_profile_for(request.user)
        form.fields["doctor"].queryset = DoctorProfile.objects.filter(pk=profile.pk) if profile else DoctorProfile.objects.none()
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.organization = request.user.organization
        item.created_by = request.user
        if request.user.role == User.Role.DOCTOR:
            item.doctor = doctor_profile_for(request.user)
        if item.doctor and item.doctor.organization_id != request.user.organization_id:
            raise PermissionDenied
        item.save()
        _audit(request, "followup.created", item, "Created follow-up")
        messages.success(request, "Follow-up added.")
        return redirect("followups")
    return render(request, "crm/form_page.html", {"form": form, "title": "Add follow-up", "subtitle": "Keep the next step from being missed", "cancel_url": "followups", "submit_label": "Save follow-up"})


@login_required
@organization_required
@require_POST
def followup_complete(request, pk):
    if request.user.role == User.Role.BILLING:
        raise PermissionDenied
    item = get_object_or_404(visible_followups(request.user), pk=pk)
    item.status = FollowUp.Status.DONE
    item.save(update_fields=["status"])
    _audit(request, "followup.completed", item, "Completed follow-up")
    messages.success(request, "Follow-up marked as done.")
    return redirect("followups")


@login_required
@organization_required
def invoice_list(request):
    qs = visible_invoices(request.user)
    status = request.GET.get("status", "")
    q = request.GET.get("q", "").strip()
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(patient__first_name__icontains=q) | Q(patient__last_name__icontains=q) | Q(invoice_number__icontains=q) | Q(patient__mrn__icontains=q))
    page_obj, querystring = _paginate(request, qs.order_by("-issue_date"))
    return render(request, "crm/billing.html", {"invoices": page_obj.object_list, "page_obj": page_obj, "querystring": querystring, "filters": request.GET, "statuses": Invoice.Status.choices})


@login_required
@organization_required
def invoice_create(request):
    if request.user.role not in [User.Role.CLINIC_MANAGER, User.Role.DOCTOR, User.Role.BILLING]:
        raise PermissionDenied
    form = InvoiceForm(request.POST or None)
    form.fields["patient"].queryset = visible_patients(request.user)
    form.fields["doctor"].queryset = DoctorProfile.objects.filter(organization=request.user.organization, is_active=True)
    form.fields["appointment"].queryset = visible_appointments(request.user)
    if request.user.role == User.Role.DOCTOR:
        profile = doctor_profile_for(request.user)
        form.fields["doctor"].queryset = DoctorProfile.objects.filter(pk=profile.pk) if profile else DoctorProfile.objects.none()
    if request.method == "POST" and form.is_valid():
        invoice = form.save(commit=False)
        invoice.organization = request.user.organization
        invoice.created_by = request.user
        if request.user.role == User.Role.DOCTOR:
            invoice.doctor = doctor_profile_for(request.user)
        if invoice.patient.organization_id != request.user.organization_id:
            raise PermissionDenied
        if invoice.doctor and invoice.doctor.organization_id != request.user.organization_id:
            raise PermissionDenied
        invoice.save()
        _audit(request, "invoice.created", invoice, "Created invoice")
        messages.success(request, "Invoice created.")
        return redirect("invoice_list")
    return render(request, "crm/form_page.html", {"form": form, "title": "Create invoice", "subtitle": "Add a charge to the patient account", "cancel_url": "invoice_list", "submit_label": "Create invoice"})


@login_required
@organization_required
def invoice_detail(request, pk):
    invoice = get_object_or_404(visible_invoices(request.user), pk=pk)
    form = PaymentForm(request.POST or None)
    if request.method == "POST":
        if request.user.role not in [User.Role.CLINIC_MANAGER, User.Role.DOCTOR, User.Role.BILLING]:
            raise PermissionDenied
        if form.is_valid():
            with transaction.atomic():
                locked = Invoice.objects.select_for_update().get(pk=invoice.pk, organization=request.user.organization)
                amount = form.cleaned_data["amount"]
                if locked.status in {Invoice.Status.VOID, Invoice.Status.DRAFT}:
                    form.add_error(None, "A draft or void invoice cannot receive payments.")
                elif amount > locked.balance:
                    form.add_error("amount", f"Payment exceeds the outstanding balance of ₹{locked.balance}.")
                else:
                    Payment.objects.create(invoice=locked, amount=amount, method=form.cleaned_data["method"], reference=form.cleaned_data["reference"], notes=form.cleaned_data["notes"], received_by=request.user)
                    locked.amount_paid += amount
                    locked.save()
                    _audit(request, "payment.received", locked, "Recorded invoice payment")
                    messages.success(request, "Payment recorded.")
                    return redirect("invoice_detail", pk=locked.pk)
    return render(request, "crm/invoice_detail.html", {"invoice": invoice, "form": form})


@login_required
@organization_required
def doctor_list(request):
    doctors = DoctorProfile.objects.filter(organization=request.user.organization).select_related("user")
    q = request.GET.get("q", "").strip()
    specialty = request.GET.get("specialty", "").strip()
    if q:
        doctors = doctors.filter(Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q) | Q(user__email__icontains=q))
    if specialty:
        doctors = doctors.filter(specialty__icontains=specialty)
    specialties = DoctorProfile.objects.filter(organization=request.user.organization).values_list("specialty", flat=True).distinct()
    page_obj, querystring = _paginate(request, doctors)
    return render(request, "crm/doctors.html", {"doctors": page_obj.object_list, "page_obj": page_obj, "querystring": querystring, "filters": request.GET, "specialties": specialties})


@login_required
@organization_required
@role_required(User.Role.CLINIC_MANAGER)
def staff_list(request):
    if request.user.organization.account_type == Organization.AccountType.INDIVIDUAL:
        raise PermissionDenied("Individual doctor workspaces do not have team management.")
    users = User.objects.filter(organization=request.user.organization).exclude(role__in=PLATFORM_ROLES).order_by("role", "first_name")
    page_obj, querystring = _paginate(request, users)
    return render(request, "crm/staff.html", {"staff": page_obj.object_list, "page_obj": page_obj, "querystring": querystring})


@login_required
@organization_required
@role_required(User.Role.CLINIC_MANAGER)
def staff_create(request):
    if request.user.organization.account_type == Organization.AccountType.INDIVIDUAL:
        messages.error(request, "Individual doctor workspaces do not support additional staff accounts.")
        return redirect("staff")
    form = StaffUserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        doctor_limit = settings.CARE_COVERY_MAX_DOCTORS_PER_CLINIC
        if form.cleaned_data["role"] == User.Role.DOCTOR and doctor_limit and request.user.organization.doctors.count() >= doctor_limit:
            form.add_error("role", f"This clinic has reached its configured limit of {doctor_limit} doctor accounts. Contact CareCovery support to review the plan limit.")
        else:
            with transaction.atomic():
                user = form.save(commit=False)
                user.organization = request.user.organization
                user.set_password(secrets.token_urlsafe(48))
                user.save()
                if user.role == User.Role.DOCTOR:
                    DoctorProfile.objects.create(
                        user=user, organization=user.organization,
                        specialty=form.cleaned_data.get("specialty") or "General Practitioner",
                        qualifications=form.cleaned_data.get("qualifications", ""),
                        registration_number=form.cleaned_data.get("registration_number", ""),
                        consultation_fee=form.cleaned_data.get("consultation_fee") or 0,
                    )
                _audit(request, "staff.created", user, f"Created {user.get_role_display()} account")
                _send_account_setup_email_after_commit(request, user)
            messages.success(request, f"Team member account created. Password setup email queued for {user.email}.")
            return redirect("staff")
    return render(request, "crm/staff_create.html", {"form": form})


@login_required
@organization_required
@role_required(User.Role.CLINIC_MANAGER)
@require_POST
def staff_status(request, pk):
    if request.user.organization.account_type == Organization.AccountType.INDIVIDUAL:
        raise PermissionDenied
    requested = request.POST.get("is_active")
    if requested not in {"0", "1"}:
        raise PermissionDenied("Invalid account status.")
    new_value = requested == "1"
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=request.user.organization_id)
        target = get_object_or_404(User.objects.select_for_update(), pk=pk, organization=request.user.organization)
        if target.pk == request.user.pk:
            raise PermissionDenied("You cannot disable your own account from this screen.")
        if not new_value and target.role == User.Role.CLINIC_MANAGER:
            active_admins = User.objects.filter(organization=request.user.organization, role=User.Role.CLINIC_MANAGER, is_active=True).count()
            if active_admins <= 1:
                raise PermissionDenied("A clinic must retain at least one active administrator.")
        target.is_active = new_value
        target.save(update_fields=["is_active"])
        _audit(request, "staff.status_changed", target, f"Account access {'enabled' if new_value else 'disabled'}")
    messages.success(request, f"{target.get_full_name() or target.username} account {'enabled' if new_value else 'disabled'}.")
    return redirect("staff")


@login_required
@role_required(*PLATFORM_ROLES)
def organization_list(request):
    Organization.expire_overdue()
    qs = Organization.objects.all().select_related("created_by")
    if request.user.role == User.Role.ONBOARDING_AGENT:
        qs = qs.filter(created_by=request.user)
    q = request.GET.get("q", "").strip()
    kind = request.GET.get("type", "")
    status = request.GET.get("status", "")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(phone__icontains=q))
    if kind:
        qs = qs.filter(account_type=kind)
    if status:
        qs = qs.filter(status=status)
    page_obj, querystring = _paginate(request, qs.order_by("-created_at"))
    return render(request, "crm/organizations.html", {"organizations": page_obj.object_list, "page_obj": page_obj, "querystring": querystring, "filters": request.GET, "account_types": Organization.AccountType.choices, "statuses": Organization.Status.choices})


@login_required
@role_required(*PLATFORM_ROLES)
def organization_create(request):
    allow_license = request.user.role == User.Role.SUPER_ADMIN
    form = OrganizationForm(request.POST or None, allow_license=allow_license)
    if request.method == "POST" and form.is_valid():
        access_type = form.cleaned_data["access_type"]
        if request.user.role == User.Role.ONBOARDING_AGENT and access_type != Organization.SubscriptionKind.TRIAL:
            form.add_error("access_type", "Onboarding agents can create trial workspaces only. A Super Admin must grant paid licenses.")
            return render(request, "crm/form_page.html", {"form": form, "title": "Onboard a workspace", "subtitle": "Create an individual doctor account or a multi-doctor clinic", "cancel_url": "organizations", "submit_label": "Create workspace"})
        with transaction.atomic():
            org = form.save(commit=False)
            org.created_by = request.user
            org.save()
            org.grant_access(access_type, form.cleaned_data["duration_days"])
            owner_role = User.Role.DOCTOR if org.account_type == Organization.AccountType.INDIVIDUAL else User.Role.CLINIC_MANAGER
            owner = User.objects.create_user(
                username=form.cleaned_data["owner_username"],
                email=form.cleaned_data["owner_email"],
                password=secrets.token_urlsafe(48),
                first_name=form.cleaned_data["owner_first_name"],
                last_name=form.cleaned_data.get("owner_last_name", ""),
                phone=form.cleaned_data.get("owner_phone", ""),
                role=owner_role,
                organization=org,
            )
            if owner_role == User.Role.DOCTOR:
                DoctorProfile.objects.create(user=owner, organization=org)
            _audit(request, "organization.created", org, f"Created {org.account_type} workspace with {access_type} access for {form.cleaned_data['duration_days']} days")
            _send_account_setup_email_after_commit(request, owner)
        messages.success(request, f"Workspace created with {org.get_subscription_kind_display().lower()} access until {timezone.localtime(org.access_expires_at):%d %b %Y}. Account setup email queued for {owner.email}.")
        return redirect("organization_detail", pk=org.pk)
    subtitle = "Create a workspace and choose its trial or license duration. Onboarding Agents can create trials; Super Admins can grant trials or paid licenses."
    return render(request, "crm/form_page.html", {"form": form, "title": "Onboard a workspace", "subtitle": subtitle, "cancel_url": "organizations", "submit_label": "Create workspace"})


@login_required
@role_required(User.Role.SUPER_ADMIN)
@require_POST
def organization_status(request, pk):
    new_status = request.POST.get("status")
    if new_status != Organization.Status.SUSPENDED:
        raise PermissionDenied("Use the subscription form to grant a time-limited trial or license.")
    with transaction.atomic():
        org = get_object_or_404(Organization.objects.select_for_update(), pk=pk)
        org.status = Organization.Status.SUSPENDED
        org.suspension_reason = "Suspended manually by CareCovery Super Admin"
        org.save(update_fields=["status", "suspension_reason", "updated_at"])
        _audit(request, "organization.suspended", org, "Workspace manually suspended by Super Admin")
    messages.success(request, "Workspace suspended. Grant a new trial or license to restore access.")
    return redirect("organization_detail", pk=org.pk)


@login_required
@role_required(User.Role.SUPER_ADMIN)
@require_POST
def organization_subscription(request, pk):
    subscription_kind = request.POST.get("subscription_kind", "")
    try:
        duration_days = int(request.POST.get("duration_days", ""))
    except (TypeError, ValueError):
        raise PermissionDenied("Enter a valid subscription duration.")
    if subscription_kind not in Organization.SubscriptionKind.values or not 1 <= duration_days <= 3650:
        raise PermissionDenied("Choose a valid trial/license type and duration between 1 and 3650 days.")
    with transaction.atomic():
        org = get_object_or_404(Organization.objects.select_for_update(), pk=pk)
        previous_expiry = org.access_expires_at
        org.grant_access(subscription_kind, duration_days)
        _audit(
            request, "organization.subscription_granted", org,
            f"Granted {subscription_kind} access for {duration_days} days; previous expiry {previous_expiry or 'not set'}",
        )
    messages.success(request, f"{org.get_subscription_kind_display()} granted for {duration_days} days, until {timezone.localtime(org.access_expires_at):%d %b %Y}.")
    return redirect("organization_detail", pk=org.pk)


@login_required
@role_required(*PLATFORM_ROLES)
def organization_detail(request, pk):
    qs = Organization.objects.all()
    if request.user.role == User.Role.ONBOARDING_AGENT:
        qs = qs.filter(created_by=request.user)
    org = get_object_or_404(qs, pk=pk)
    org.expire_if_needed()
    return render(request, "crm/organization_detail.html", {
        "org": org,
        "members": org.users.exclude(role__in=PLATFORM_ROLES).order_by("role", "first_name"),
        "doctors": org.doctors.select_related("user"),
        "patients_count": org.patients.count(),
        "appointments_count": org.appointments.count(),
    })


@login_required
@organization_required
@require_POST
def notification_read(request, pk):
    notification = get_object_or_404(Notification, user=request.user, pk=pk)
    notification.is_read = True
    notification.save(update_fields=["is_read"])
    target = notification.url or ""
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(target)
    return redirect("dashboard")


@login_required
@organization_required
def audit_log(request):
    if request.user.role != User.Role.CLINIC_MANAGER:
        raise PermissionDenied
    logs = AuditLog.objects.filter(organization=request.user.organization).select_related("actor")[:200]
    page_obj, querystring = _paginate(request, logs)
    return render(request, "crm/audit.html", {"logs": page_obj.object_list, "page_obj": page_obj, "querystring": querystring})


@login_required
@organization_required
def reports(request):
    if request.user.role in [User.Role.NURSE, User.Role.BILLING]:
        raise PermissionDenied
    appointments = visible_appointments(request.user)
    patients = visible_patients(request.user)
    invoices = visible_invoices(request.user)
    today = timezone.localdate()
    month_start = today.replace(day=1)
    context = {
        "new_patients_month": patients.filter(created_at__date__gte=month_start).count(),
        "appointments_month": appointments.filter(starts_at__date__gte=month_start, starts_at__date__lte=today).count(),
        "completed_month": appointments.filter(status=Appointment.Status.COMPLETED, starts_at__date__gte=month_start).count(),
        "revenue_month": invoices.filter(issue_date__gte=month_start).aggregate(total=Sum("amount_paid"))["total"] or Decimal("0"),
        "expenses_month": (PracticeExpense.objects.filter(organization=request.user.organization, expense_date__gte=month_start, expense_date__lte=today).aggregate(total=Sum("amount"))["total"] or Decimal("0")) if request.user.role == User.Role.CLINIC_MANAGER or (request.user.role == User.Role.DOCTOR and request.user.organization.account_type == Organization.AccountType.INDIVIDUAL) else Decimal("0"),
        "low_stock_count": InventoryItem.objects.filter(organization=request.user.organization, is_active=True, quantity__lte=F("reorder_level")).count(),
        "outstanding": sum((inv.balance for inv in invoices.exclude(status__in=[Invoice.Status.PAID, Invoice.Status.VOID])), Decimal("0.00")),
        "top_doctors": DoctorProfile.objects.filter(organization=request.user.organization).annotate(patient_total=Count("primary_patients")).order_by("-patient_total")[:5] if request.user.role == User.Role.CLINIC_MANAGER else [],
    }
    return render(request, "crm/reports.html", context)


@login_required
@organization_required
def queue_list(request):
    if request.user.role == User.Role.BILLING:
        raise PermissionDenied
    today = timezone.localdate()
    qs = QueueEntry.objects.filter(organization=request.user.organization, queue_date=today).select_related("patient", "doctor__user", "appointment")
    if request.user.role == User.Role.DOCTOR:
        profile = doctor_profile_for(request.user)
        qs = qs.filter(doctor=profile) if profile else qs.none()
    status = request.GET.get("status", "")
    doctor = request.GET.get("doctor", "")
    if status:
        qs = qs.filter(status=status)
    if doctor and request.user.role != User.Role.DOCTOR:
        qs = qs.filter(doctor_id=doctor)
    doctors = DoctorProfile.objects.filter(organization=request.user.organization, is_active=True).select_related("user")
    qs = qs.annotate(priority_order=Case(
        When(priority="emergency", then=Value(0)),
        When(priority="urgent", then=Value(1)),
        default=Value(2), output_field=IntegerField(),
    ))
    page_obj, querystring = _paginate(request, qs.order_by("priority_order", "token_number"))
    return render(request, "crm/queue.html", {"entries": page_obj.object_list, "page_obj": page_obj, "querystring": querystring, "doctors": doctors, "filters": request.GET, "today": today})


@login_required
@organization_required
@require_POST
def queue_add(request, appointment_id):
    if request.user.role == User.Role.BILLING:
        raise PermissionDenied
    appointment = get_object_or_404(visible_appointments(request.user), pk=appointment_id)
    if timezone.localtime(appointment.starts_at).date() != timezone.localdate():
        messages.error(request, "Only today's appointments can be added to today's queue.")
        return redirect("appointments")
    if appointment.status not in [Appointment.Status.CONFIRMED, Appointment.Status.CHECKED_IN]:
        messages.error(request, "Only confirmed or checked-in appointments can enter the queue.")
        return redirect("appointments")
    with transaction.atomic():
        # Serialize token allocation per workspace to avoid duplicate daily tokens
        # under simultaneous check-ins (PostgreSQL production database).
        Organization.objects.select_for_update().get(pk=request.user.organization_id)
        next_token = (QueueEntry.objects.filter(
            organization=request.user.organization, queue_date=timezone.localdate()
        ).order_by("-token_number").values_list("token_number", flat=True).first() or 0) + 1
        entry, created = QueueEntry.objects.get_or_create(
            appointment=appointment,
            defaults={
                "organization": request.user.organization,
                "patient": appointment.patient,
                "doctor": appointment.doctor,
                "queue_date": timezone.localdate(),
                "token_number": next_token,
                "priority": "emergency" if appointment.kind == Appointment.Kind.EMERGENCY else "normal",
            },
        )
    if created:
        if appointment.status == Appointment.Status.CONFIRMED:
            appointment.status = Appointment.Status.CHECKED_IN
            appointment.save(update_fields=["status"])
        _audit(request, "queue.added", entry, "Added appointment to queue")
        messages.success(request, f"Added to queue with token #{entry.token_number}.")
    else:
        messages.info(request, "This appointment is already in the queue.")
    return redirect("queue")


@login_required
@organization_required
@require_POST
def queue_status(request, pk):
    if request.user.role == User.Role.BILLING:
        raise PermissionDenied
    qs = QueueEntry.objects.filter(organization=request.user.organization)
    if request.user.role == User.Role.DOCTOR:
        qs = qs.filter(doctor__user=request.user)
    entry = get_object_or_404(qs, pk=pk)
    new_status = request.POST.get("status")
    allowed = {
        QueueEntry.Status.WAITING: {QueueEntry.Status.WITH_DOCTOR, QueueEntry.Status.LEFT},
        QueueEntry.Status.WITH_DOCTOR: {QueueEntry.Status.COMPLETED, QueueEntry.Status.LEFT},
        QueueEntry.Status.COMPLETED: set(),
        QueueEntry.Status.LEFT: set(),
    }
    if new_status not in allowed[entry.status]:
        raise PermissionDenied
    entry.status = new_status
    if new_status == QueueEntry.Status.WITH_DOCTOR:
        entry.called_at = timezone.now()
        if entry.appointment:
            entry.appointment.status = Appointment.Status.IN_PROGRESS
            entry.appointment.save(update_fields=["status"])
    if new_status == QueueEntry.Status.COMPLETED:
        entry.completed_at = timezone.now()
        if entry.appointment:
            entry.appointment.status = Appointment.Status.COMPLETED
            entry.appointment.save(update_fields=["status"])
    entry.save()
    _audit(request, "queue.status_changed", entry, f"Queue status changed to {new_status}")
    return redirect("queue")


@require_GET
def health_check(request):
    """Readiness probe: verify PostgreSQL and apply due workspace expirations."""
    try:
        # Query a real application table: external uptime monitoring of this
        # endpoint creates database activity on the configured PostgreSQL
        # provider (e.g. Supabase), rather than merely checking web-process health.
        Organization.objects.order_by("id").values_list("id", flat=True).first()
        Organization.expire_overdue()
    except Exception:
        logger.exception("CareCovery readiness check failed")
        response = JsonResponse({"status": "unavailable", "database": "error"}, status=503)
        response["Cache-Control"] = "no-store"
        return response
    response = JsonResponse({"status": "ok", "database": "ok", "database_activity": "query_succeeded"})
    response["Cache-Control"] = "no-store"
    return response


@login_required
@organization_required
def expense_list(request):
    allowed = {User.Role.CLINIC_MANAGER, User.Role.BILLING}
    if request.user.role == User.Role.DOCTOR and request.user.organization.account_type == Organization.AccountType.INDIVIDUAL:
        allowed.add(User.Role.DOCTOR)
    if request.user.role not in allowed:
        raise PermissionDenied
    qs = PracticeExpense.objects.filter(organization=request.user.organization).select_related("created_by")
    q = request.GET.get("q", "").strip()
    category = request.GET.get("category", "")
    start = request.GET.get("start", "")
    end = request.GET.get("end", "")
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(vendor__icontains=q) | Q(reference__icontains=q))
    if category:
        qs = qs.filter(category=category)
    if start:
        try:
            qs = qs.filter(expense_date__gte=date.fromisoformat(start))
        except ValueError:
            messages.error(request, "Enter a valid expense start date.")
    if end:
        try:
            qs = qs.filter(expense_date__lte=date.fromisoformat(end))
        except ValueError:
            messages.error(request, "Enter a valid expense end date.")
    page_obj, querystring = _paginate(request, qs)
    total = qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    return render(request, "crm/expenses.html", {"expenses": page_obj.object_list, "page_obj": page_obj, "querystring": querystring, "filters": request.GET, "categories": PracticeExpense.Category.choices, "total": total})


@login_required
@organization_required
def expense_create(request):
    allowed = {User.Role.CLINIC_MANAGER, User.Role.BILLING}
    if request.user.role == User.Role.DOCTOR and request.user.organization.account_type == Organization.AccountType.INDIVIDUAL:
        allowed.add(User.Role.DOCTOR)
    if request.user.role not in allowed:
        raise PermissionDenied
    form = PracticeExpenseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        expense = form.save(commit=False)
        expense.organization = request.user.organization
        expense.created_by = request.user
        expense.save()
        _audit(request, "expense.created", expense, "Recorded practice expense")
        messages.success(request, "Expense recorded.")
        return redirect("expenses")
    return render(request, "crm/form_page.html", {"form": form, "title": "Add expense", "subtitle": "Track clinic or practice operating costs", "cancel_url": "expenses", "submit_label": "Save expense"})


@login_required
@organization_required
def inventory_list(request):
    qs = InventoryItem.objects.filter(organization=request.user.organization, is_active=True)
    q = request.GET.get("q", "").strip()
    category = request.GET.get("category", "")
    stock = request.GET.get("stock", "")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(sku__icontains=q) | Q(storage_location__icontains=q))
    if category:
        qs = qs.filter(category=category)
    if stock == "low":
        qs = qs.filter(quantity__lte=F("reorder_level"))
    elif stock == "healthy":
        qs = qs.filter(quantity__gt=F("reorder_level"))
    page_obj, querystring = _paginate(request, qs)
    can_manage = request.user.role in {User.Role.CLINIC_MANAGER, User.Role.BILLING} or (request.user.role == User.Role.DOCTOR and request.user.organization.account_type == Organization.AccountType.INDIVIDUAL)
    return render(request, "crm/inventory.html", {"items": page_obj.object_list, "page_obj": page_obj, "querystring": querystring, "filters": request.GET, "categories": InventoryItem.Category.choices, "can_manage": can_manage, "low_stock_count": qs.filter(quantity__lte=F("reorder_level")).count()})


@login_required
@organization_required
def inventory_item_create(request):
    allowed = {User.Role.CLINIC_MANAGER, User.Role.BILLING}
    if request.user.role == User.Role.DOCTOR and request.user.organization.account_type == Organization.AccountType.INDIVIDUAL:
        allowed.add(User.Role.DOCTOR)
    if request.user.role not in allowed:
        raise PermissionDenied
    form = InventoryItemForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        sku = form.cleaned_data.get("sku", "").strip()
        if sku and InventoryItem.objects.filter(organization=request.user.organization, sku__iexact=sku).exists():
            form.add_error("sku", "This SKU is already used in this workspace.")
        else:
            item = form.save(commit=False)
            item.organization = request.user.organization
            item.save()
            if item.quantity > 0:
                InventoryMovement.objects.create(organization=request.user.organization, item=item, movement_type=InventoryMovement.MovementType.STOCK_IN, quantity=item.quantity, balance_after=item.quantity, note="Opening stock", created_by=request.user)
            _audit(request, "inventory.item_created", item, "Added inventory item")
            messages.success(request, "Inventory item added.")
            return redirect("inventory")
    return render(request, "crm/form_page.html", {"form": form, "title": "Add inventory item", "subtitle": "Set the opening stock and low-stock threshold", "cancel_url": "inventory", "submit_label": "Save item"})


@login_required
@organization_required
@require_POST
def inventory_movement(request, pk):
    allowed = {User.Role.CLINIC_MANAGER, User.Role.BILLING}
    if request.user.role == User.Role.DOCTOR and request.user.organization.account_type == Organization.AccountType.INDIVIDUAL:
        allowed.add(User.Role.DOCTOR)
    if request.user.role not in allowed:
        raise PermissionDenied
    form = InventoryMovementForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Check the stock movement details and try again.")
        return redirect("inventory")
    movement_type = form.cleaned_data["movement_type"]
    quantity = form.cleaned_data["quantity"]
    if movement_type in {InventoryMovement.MovementType.STOCK_IN, InventoryMovement.MovementType.STOCK_OUT} and quantity <= 0:
        messages.error(request, "Stock received/used quantity must be greater than zero.")
        return redirect("inventory")
    with transaction.atomic():
        item = get_object_or_404(InventoryItem.objects.select_for_update(), pk=pk, organization=request.user.organization, is_active=True)
        if movement_type == InventoryMovement.MovementType.STOCK_IN:
            new_balance = item.quantity + quantity
        elif movement_type == InventoryMovement.MovementType.STOCK_OUT:
            if quantity > item.quantity:
                messages.error(request, f"Only {item.quantity} {item.unit} are currently available.")
                return redirect("inventory")
            new_balance = item.quantity - quantity
        else:
            new_balance = quantity
        item.quantity = new_balance
        item.save(update_fields=["quantity", "updated_at"])
        InventoryMovement.objects.create(organization=request.user.organization, item=item, movement_type=movement_type, quantity=quantity, balance_after=new_balance, note=form.cleaned_data["note"], created_by=request.user)
        _audit(request, "inventory.stock_changed", item, f"Inventory stock {movement_type}")
    messages.success(request, f"Stock updated. Current balance: {new_balance} {item.unit}.")
    return redirect("inventory")


@login_required
@organization_required
def inventory_history(request, pk):
    item = get_object_or_404(InventoryItem, pk=pk, organization=request.user.organization)
    movements = item.movements.select_related("created_by")[:100]
    return render(request, "crm/inventory_history.html", {"item": item, "movements": movements})


def _csv_cell(value):
    text_value = "" if value is None else str(value)
    if text_value.lstrip(" \t\r\n\x00\x0b\x0c")[:1] in {"=", "+", "-", "@"}:
        return "'" + text_value
    return text_value


@login_required
@organization_required
def export_csv(request, resource):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="carecovery-{resource}-{timezone.localdate():%Y%m%d}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    if resource == "patients":
        if request.user.role in {User.Role.BILLING, User.Role.NURSE}:
            raise PermissionDenied
        qs = visible_patients(request.user).select_related("primary_doctor__user")[:10000]
        writer.writerow(["MRN", "First name", "Last name", "Date of birth", "Gender", "Phone", "Email", "Blood group", "Primary doctor", "Created"])
        for obj in qs:
            writer.writerow([_csv_cell(v) for v in [obj.mrn, obj.first_name, obj.last_name, obj.date_of_birth, obj.gender, obj.phone, obj.email, obj.blood_group, obj.primary_doctor.user.get_full_name() if obj.primary_doctor else "", obj.created_at]])
    elif resource == "appointments":
        if request.user.role in {User.Role.NURSE, User.Role.BILLING}:
            raise PermissionDenied
        qs = visible_appointments(request.user).select_related("patient", "doctor__user")[:10000]
        writer.writerow(["Patient MRN", "Patient", "Doctor", "Starts at", "Duration minutes", "Type", "Status", "Fee", "Reason"])
        for obj in qs:
            writer.writerow([_csv_cell(v) for v in [obj.patient.mrn, obj.patient.full_name, obj.doctor.user.get_full_name(), obj.starts_at, obj.duration_minutes, obj.get_kind_display(), obj.get_status_display(), obj.fee, obj.reason]])
    elif resource == "invoices":
        if request.user.role == User.Role.NURSE:
            raise PermissionDenied
        qs = visible_invoices(request.user).select_related("patient")[:10000]
        writer.writerow(["Invoice", "Patient MRN", "Patient", "Issue date", "Subtotal", "Discount", "Tax", "Total", "Paid", "Balance", "Status"])
        for obj in qs:
            writer.writerow([_csv_cell(v) for v in [obj.invoice_number, obj.patient.mrn, obj.patient.full_name, obj.issue_date, obj.subtotal, obj.discount, obj.tax, obj.total, obj.amount_paid, obj.balance, obj.get_status_display()]])
    elif resource == "expenses":
        allowed = {User.Role.CLINIC_MANAGER, User.Role.BILLING}
        if request.user.role == User.Role.DOCTOR and request.user.organization.account_type == Organization.AccountType.INDIVIDUAL:
            allowed.add(User.Role.DOCTOR)
        if request.user.role not in allowed:
            raise PermissionDenied
        qs = PracticeExpense.objects.filter(organization=request.user.organization).order_by("-expense_date")[:10000]
        writer.writerow(["Date", "Category", "Title", "Vendor", "Amount", "Payment method", "Reference"])
        for obj in qs:
            writer.writerow([_csv_cell(v) for v in [obj.expense_date, obj.get_category_display(), obj.title, obj.vendor, obj.amount, obj.get_payment_method_display(), obj.reference]])
    elif resource == "inventory":
        allowed_inventory_export = {User.Role.CLINIC_MANAGER, User.Role.BILLING}
        if request.user.role == User.Role.DOCTOR and request.user.organization.account_type == Organization.AccountType.INDIVIDUAL:
            allowed_inventory_export.add(User.Role.DOCTOR)
        if request.user.role not in allowed_inventory_export:
            raise PermissionDenied
        qs = InventoryItem.objects.filter(organization=request.user.organization, is_active=True).order_by("name")[:10000]
        writer.writerow(["Name", "SKU", "Category", "Quantity", "Unit", "Reorder level", "Unit cost", "Expiry date", "Storage location"])
        for obj in qs:
            writer.writerow([_csv_cell(v) for v in [obj.name, obj.sku, obj.get_category_display(), obj.quantity, obj.unit, obj.reorder_level, obj.unit_cost, obj.expiry_date, obj.storage_location]])
    elif resource == "patients-template":
        writer.writerow(["first_name", "last_name", "phone", "email", "date_of_birth", "gender", "blood_group", "allergies", "chronic_conditions", "tags", "doctor_username"])
    else:
        raise PermissionDenied
    _audit(request, "data.exported", None, f"Exported {resource} CSV")
    return response


@login_required
@organization_required
def patient_import(request):
    if request.user.role not in {User.Role.CLINIC_MANAGER, User.Role.DOCTOR}:
        raise PermissionDenied
    if request.method == "POST":
        form = PatientImportForm(request.POST)
        if form.is_valid():
            content = form.cleaned_data["csv_text"].lstrip("\ufeff")
            reader = csv.DictReader(io.StringIO(content))
            required = {"first_name", "last_name", "phone", "email", "date_of_birth", "gender", "blood_group", "allergies", "chronic_conditions", "tags"}
            headers = {str(h or "").strip().lower() for h in (reader.fieldnames or [])}
            if not required.issubset(headers):
                missing = ", ".join(sorted(required - headers))
                form.add_error("csv_text", f"Missing required columns: {missing}")
                return render(request, "crm/patient_import.html", {"form": form})
            rows = list(reader)
            if len(rows) > 1000:
                form.add_error("csv_text", "A single import can contain at most 1,000 data rows.")
                return render(request, "crm/patient_import.html", {"form": form})
            profile = doctor_profile_for(request.user) if request.user.role == User.Role.DOCTOR else None
            if request.user.role == User.Role.DOCTOR and not profile:
                raise PermissionDenied("Doctor profile is not configured.")
            imported, skipped, errors = 0, 0, []
            valid_genders = {choice[0] for choice in Patient.Gender.choices}
            with transaction.atomic():
                for line_number, row in enumerate(rows, start=2):
                    row = {str(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k is not None}
                    first_name = row.get("first_name", "")
                    last_name = row.get("last_name", "")
                    phone = row.get("phone", "")
                    if not first_name:
                        skipped += 1
                        errors.append(f"Row {line_number}: first_name is required.")
                        continue
                    if len(first_name) > 80 or len(last_name) > 80 or len(phone) > 30 or len(row.get("tags", "")) > 255:
                        skipped += 1
                        errors.append(f"Row {line_number}: a name or phone value is too long.")
                        continue
                    if phone and Patient.objects.filter(organization=request.user.organization, phone=phone).exists():
                        skipped += 1
                        errors.append(f"Row {line_number}: phone {phone} already exists in this workspace.")
                        continue
                    dob = None
                    if row.get("date_of_birth"):
                        try:
                            dob = date.fromisoformat(row["date_of_birth"])
                        except ValueError:
                            skipped += 1
                            errors.append(f"Row {line_number}: date_of_birth must use YYYY-MM-DD.")
                            continue
                        if dob > timezone.localdate():
                            skipped += 1
                            errors.append(f"Row {line_number}: date_of_birth cannot be in the future.")
                            continue
                    gender = row.get("gender", "").lower()
                    if gender and gender not in valid_genders:
                        skipped += 1
                        errors.append(f"Row {line_number}: gender must be one of {', '.join(sorted(valid_genders))}.")
                        continue
                    email = row.get("email", "")
                    if len(email) > 254:
                        skipped += 1
                        errors.append(f"Row {line_number}: email is too long.")
                        continue
                    if email:
                        try:
                            validate_email(email)
                        except ValidationError:
                            skipped += 1
                            errors.append(f"Row {line_number}: email is not valid.")
                            continue
                    primary_doctor = profile
                    doctor_username = row.get("doctor_username", "")
                    if request.user.role != User.Role.DOCTOR and doctor_username:
                        primary_doctor = DoctorProfile.objects.filter(organization=request.user.organization, user__username=doctor_username, is_active=True).first()
                        if not primary_doctor:
                            skipped += 1
                            errors.append(f"Row {line_number}: doctor_username was not found in this workspace.")
                            continue
                    patient = Patient.objects.create(
                        organization=request.user.organization, first_name=first_name, last_name=last_name,
                        date_of_birth=dob, gender=gender, phone=phone, email=email,
                        blood_group=row.get("blood_group", "")[:5], allergies=row.get("allergies", ""),
                        chronic_conditions=row.get("chronic_conditions", ""), tags=row.get("tags", ""),
                        primary_doctor=primary_doctor, created_by=request.user,
                    )
                    if primary_doctor:
                        PatientAccess.objects.get_or_create(patient=patient, doctor=primary_doctor, defaults={"granted_by": request.user, "reason": "CSV import assignment"})
                    imported += 1
            _audit(request, "patients.imported", None, f"Imported {imported} patient(s); skipped {skipped} row(s).")
            return render(request, "crm/patient_import.html", {"form": PatientImportForm(), "imported": imported, "skipped": skipped, "errors": errors[:20], "done": True})
    else:
        form = PatientImportForm()
    return render(request, "crm/patient_import.html", {"form": form})
