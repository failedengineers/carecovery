from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone

from .models import (
    Patient, Appointment, MedicalRecord, FollowUp, Invoice,
    User, DoctorProfile, Organization, PracticeExpense, InventoryItem,
)

UserModel = get_user_model()
BASE_INPUT = "field-input"


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.update({"class": "field-check"})
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.update({"class": "field-input field-select"})
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs.update({"class": "field-input", "rows": 3})
            else:
                field.widget.attrs.update({"class": BASE_INPUT})
            if field.required:
                field.widget.attrs.setdefault("aria-required", "true")


class PatientForm(StyledModelForm):
    class Meta:
        model = Patient
        fields = [
            "first_name", "last_name", "date_of_birth", "gender", "phone", "email",
            "blood_group", "address", "allergies", "chronic_conditions",
            "emergency_contact_name", "emergency_contact_phone", "primary_doctor",
            "tags", "notes", "consent_to_contact",
        ]
        widgets = {"date_of_birth": forms.DateInput(attrs={"type": "date"})}

    def clean_phone(self):
        phone = self.cleaned_data.get("phone", "").strip()
        if phone and len(phone) < 7:
            raise forms.ValidationError("Enter a valid phone number, or leave this blank.")
        return phone


class AppointmentForm(StyledModelForm):
    class Meta:
        model = Appointment
        fields = ["patient", "doctor", "starts_at", "duration_minutes", "kind", "reason", "room", "fee", "internal_notes"]
        widgets = {"starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}

    def clean(self):
        cleaned = super().clean()
        patient = cleaned.get("patient")
        doctor = cleaned.get("doctor")
        starts_at = cleaned.get("starts_at")
        duration = cleaned.get("duration_minutes") or 30
        status = cleaned.get("status")
        if patient and doctor and patient.organization_id != doctor.organization_id:
            self.add_error("doctor", "The selected doctor does not belong to this patient's workspace.")
        if doctor and starts_at and status not in [Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW]:
            ends_at = starts_at + timedelta(minutes=duration)
            clashes = Appointment.objects.filter(
                organization_id=doctor.organization_id,
                doctor=doctor,
                starts_at__lt=ends_at,
                status__in=[Appointment.Status.REQUESTED, Appointment.Status.CONFIRMED, Appointment.Status.CHECKED_IN, Appointment.Status.IN_PROGRESS],
            ).exclude(pk=self.instance.pk)
            for existing in clashes:
                if existing.ends_at > starts_at:
                    self.add_error("starts_at", f"This doctor already has an appointment from {timezone.localtime(existing.starts_at):%I:%M %p} to {timezone.localtime(existing.ends_at):%I:%M %p}.")
                    break
        return cleaned


class MedicalRecordForm(StyledModelForm):
    prescription_lines = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "One medicine per line: medicine | dosage | frequency | duration | instructions"}),
        help_text="Optional. Example: Paracetamol | 500 mg | Twice daily | 3 days | After food",
    )

    class Meta:
        model = MedicalRecord
        fields = [
            "patient", "appointment", "visit_date", "chief_complaint", "symptoms", "diagnosis",
            "examination", "treatment_plan", "clinical_notes", "blood_pressure", "pulse",
            "temperature", "spo2", "weight_kg", "height_cm", "follow_up_date", "is_sensitive",
        ]
        widgets = {"visit_date": forms.DateTimeInput(attrs={"type": "datetime-local"}), "follow_up_date": forms.DateInput(attrs={"type": "date"})}

    def clean(self):
        cleaned = super().clean()
        patient, appointment = cleaned.get("patient"), cleaned.get("appointment")
        if appointment and patient and appointment.patient_id != patient.pk:
            self.add_error("appointment", "Choose an appointment belonging to the selected patient.")
        if appointment and patient and appointment.organization_id != patient.organization_id:
            self.add_error("appointment", "The appointment and patient must belong to the same workspace.")
        return cleaned


class FollowUpForm(StyledModelForm):
    class Meta:
        model = FollowUp
        fields = ["patient", "doctor", "title", "due_date", "priority", "notes"]
        widgets = {"due_date": forms.DateInput(attrs={"type": "date"})}


class InvoiceForm(StyledModelForm):
    class Meta:
        model = Invoice
        fields = ["patient", "doctor", "appointment", "issue_date", "due_date", "description", "subtotal", "discount", "tax"]
        widgets = {"issue_date": forms.DateInput(attrs={"type": "date"}), "due_date": forms.DateInput(attrs={"type": "date"})}

    def clean(self):
        cleaned = super().clean()
        patient, doctor, appointment = cleaned.get("patient"), cleaned.get("doctor"), cleaned.get("appointment")
        if patient and doctor and patient.organization_id != doctor.organization_id:
            self.add_error("doctor", "Choose a doctor from the patient's workspace.")
        if appointment and patient and appointment.patient_id != patient.pk:
            self.add_error("appointment", "Choose an appointment belonging to the selected patient.")
        if appointment and doctor and appointment.doctor_id != doctor.pk:
            self.add_error("doctor", "The invoice doctor must match the selected appointment doctor.")
        if appointment and patient and appointment.organization_id != patient.organization_id:
            self.add_error("appointment", "The appointment and patient must belong to the same workspace.")
        subtotal = cleaned.get("subtotal") or Decimal("0")
        discount = cleaned.get("discount") or Decimal("0")
        if discount > subtotal:
            self.add_error("discount", "Discount cannot exceed the subtotal.")
        return cleaned


class PaymentForm(forms.Form):
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"), widget=forms.NumberInput(attrs={"class": BASE_INPUT, "step": "0.01"}))
    method = forms.ChoiceField(choices=[("cash", "Cash"), ("card", "Card"), ("upi", "UPI"), ("bank", "Bank transfer"), ("other", "Other")], widget=forms.Select(attrs={"class": "field-input field-select"}))
    reference = forms.CharField(required=False, max_length=120, widget=forms.TextInput(attrs={"class": BASE_INPUT}))
    notes = forms.CharField(required=False, max_length=255, widget=forms.TextInput(attrs={"class": BASE_INPUT}))


class OrganizationForm(StyledModelForm):
    access_type = forms.ChoiceField(
        choices=Organization.SubscriptionKind.choices,
        initial=Organization.SubscriptionKind.TRIAL,
        label="Access type",
        help_text="Choose a time-limited trial or paid license. This grants access time; it does not collect a payment.",
    )
    duration_days = forms.IntegerField(
        min_value=1, max_value=3650, initial=14,
        label="Access duration (days)",
        help_text="Enter the exact number of days to grant (1-3650). You can renew or change it later.",
    )
    owner_first_name = forms.CharField(max_length=150, label="Account owner's first name")
    owner_last_name = forms.CharField(max_length=150, required=False, label="Account owner's last name")
    owner_username = forms.CharField(max_length=150, label="Login username")
    owner_email = forms.EmailField(label="Owner email")
    owner_phone = forms.CharField(max_length=30, required=False, label="Owner phone")

    class Meta:
        model = Organization
        fields = [
            "name", "slug", "account_type", "email", "phone", "address", "timezone",
            "access_type", "duration_days", "owner_first_name", "owner_last_name",
            "owner_username", "owner_email", "owner_phone",
        ]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, allow_license=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not allow_license:
            self.fields["access_type"].choices = [(Organization.SubscriptionKind.TRIAL, "Trial")]

    def clean_owner_username(self):
        username = self.cleaned_data["owner_username"].strip()
        if UserModel.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is already in use.")
        return username

    def clean_owner_email(self):
        email = self.cleaned_data["owner_email"].strip().lower()
        if UserModel.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("This email address already belongs to an account.")
        return email

    def clean_slug(self):
        slug = self.cleaned_data["slug"].strip().lower()
        if Organization.objects.filter(slug=slug).exists():
            raise forms.ValidationError("That workspace URL key is already in use.")
        return slug


class StaffUserForm(forms.ModelForm):
    email = forms.EmailField(required=True)
    role = forms.ChoiceField(choices=[
        (User.Role.DOCTOR, "Doctor"),
        (User.Role.CLINIC_MANAGER, "Clinic Manager"),
        (User.Role.NURSE, "Nurse"),
        (User.Role.BILLING, "Billing Staff"),
    ])
    specialty = forms.CharField(required=False)
    qualifications = forms.CharField(required=False)
    registration_number = forms.CharField(required=False)
    consultation_fee = forms.DecimalField(required=False, min_value=0, max_digits=10, decimal_places=2, initial=0)

    class Meta:
        model = User
        fields = ["first_name", "last_name", "username", "email", "phone", "role"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": BASE_INPUT})

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is already in use.")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("This email address already belongs to an account.")
        return email


class PracticeExpenseForm(StyledModelForm):
    class Meta:
        model = PracticeExpense
        fields = ["category", "title", "vendor", "expense_date", "amount", "payment_method", "reference", "notes"]
        widgets = {"expense_date": forms.DateInput(attrs={"type": "date"}), "notes": forms.Textarea(attrs={"rows": 3})}


class InventoryItemForm(StyledModelForm):
    class Meta:
        model = InventoryItem
        fields = ["name", "sku", "category", "unit", "quantity", "reorder_level", "unit_cost", "expiry_date", "storage_location", "notes", "is_active"]
        widgets = {"expiry_date": forms.DateInput(attrs={"type": "date"}), "notes": forms.Textarea(attrs={"rows": 2})}


class InventoryMovementForm(forms.Form):
    movement_type = forms.ChoiceField(choices=[("stock_in", "Stock received"), ("stock_out", "Stock used"), ("adjustment", "Correct stock count")], widget=forms.Select(attrs={"class": "field-input field-select"}))
    quantity = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.00"), widget=forms.NumberInput(attrs={"class": BASE_INPUT, "step": "0.01"}), help_text="For a stock correction, enter the new total quantity on hand.")
    note = forms.CharField(required=False, max_length=255, widget=forms.TextInput(attrs={"class": BASE_INPUT}))


class PatientImportForm(forms.Form):
    """Import up to 1,000 patients by pasting CSV text; no file upload is used."""
    csv_text = forms.CharField(
        max_length=2_000_000,
        widget=forms.Textarea(attrs={
            "class": BASE_INPUT,
            "rows": 12,
            "spellcheck": "false",
            "placeholder": "first_name,last_name,phone,email,date_of_birth,gender,blood_group,allergies,chronic_conditions,tags,doctor_username\nAsha,Sharma,9876543210,asha@example.com,1990-01-12,female,O+,None,None,Regular,doctor1",
        }),
        help_text="Paste UTF-8 CSV text (maximum 2 MB and 1,000 data rows). No file is uploaded or stored.",
    )
