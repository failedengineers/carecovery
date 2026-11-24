import uuid
from decimal import Decimal
from datetime import timedelta

from django.contrib.auth.models import AbstractUser, UserManager
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone
from django.db.models.functions import Lower



class Organization(models.Model):
    """A tenant. Individual doctors and clinics each get an isolated workspace."""

    class AccountType(models.TextChoices):
        CLINIC = "clinic", "Clinic"
        INDIVIDUAL = "individual", "Individual doctor"

    class Status(models.TextChoices):
        TRIAL = "trial", "Trial"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"

    class SubscriptionKind(models.TextChoices):
        TRIAL = "trial", "Trial"
        LICENSE = "license", "Paid license"

    name = models.CharField(max_length=180)
    slug = models.SlugField(max_length=200, unique=True)
    account_type = models.CharField(max_length=20, choices=AccountType.choices, default=AccountType.CLINIC)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TRIAL, db_index=True)
    subscription_kind = models.CharField(max_length=12, choices=SubscriptionKind.choices, default=SubscriptionKind.TRIAL)
    access_started_at = models.DateTimeField(null=True, blank=True)
    access_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    suspension_reason = models.CharField(max_length=180, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    address = models.TextField(blank=True)
    timezone = models.CharField(max_length=64, default="Asia/Kolkata")
    created_by = models.ForeignKey("User", null=True, blank=True, on_delete=models.SET_NULL, related_name="onboarded_organizations")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def grant_access(self, subscription_kind, duration_days, *, starts_at=None):
        """Start a trial or paid license for an explicitly chosen duration."""
        if subscription_kind not in self.SubscriptionKind.values:
            raise ValueError("Unknown subscription type.")
        if not isinstance(duration_days, int) or not 1 <= duration_days <= 3650:
            raise ValueError("Access duration must be between 1 and 3650 days.")
        starts_at = starts_at or timezone.now()
        self.subscription_kind = subscription_kind
        self.status = self.Status.TRIAL if subscription_kind == self.SubscriptionKind.TRIAL else self.Status.ACTIVE
        self.access_started_at = starts_at
        self.access_expires_at = starts_at + timedelta(days=duration_days)
        self.suspension_reason = ""
        self.save(update_fields=[
            "subscription_kind", "status", "access_started_at",
            "access_expires_at", "suspension_reason", "updated_at",
        ])

    def expire_if_needed(self, now=None):
        """Suspend an expired trial/license on the first request after expiry."""
        now = now or timezone.now()
        if (
            self.status in {self.Status.TRIAL, self.Status.ACTIVE}
            and self.access_expires_at
            and self.access_expires_at <= now
        ):
            label = "Trial" if self.subscription_kind == self.SubscriptionKind.TRIAL else "License"
            self.status = self.Status.SUSPENDED
            self.suspension_reason = f"{label} expired on {timezone.localtime(self.access_expires_at):%d %b %Y}"
            self.save(update_fields=["status", "suspension_reason", "updated_at"])
            return True
        return False

    @classmethod
    def expire_overdue(cls, now=None):
        now = now or timezone.now()
        due = cls.objects.filter(
            status__in=[cls.Status.TRIAL, cls.Status.ACTIVE],
            access_expires_at__lte=now,
        ).only("id", "status", "subscription_kind", "access_expires_at")
        expired = 0
        for organization in due.iterator():
            expired += int(organization.expire_if_needed(now))
        return expired

    @property
    def access_days_remaining(self):
        if not self.access_expires_at:
            return None
        remaining = (self.access_expires_at - timezone.now()).total_seconds()
        if remaining <= 0:
            return 0
        from math import ceil
        return ceil(remaining / 86400)


class CareCoveryUserManager(UserManager):
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("role", "super_admin")
        extra_fields.setdefault("organization", None)
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    objects = CareCoveryUserManager()

    class Meta(AbstractUser.Meta):
        constraints = [
            models.UniqueConstraint(Lower("username"), name="unique_user_username_case_insensitive"),
            models.UniqueConstraint(Lower("email"), condition=~models.Q(email=""), name="unique_user_email_case_insensitive"),
        ]

    class Role(models.TextChoices):
        SUPER_ADMIN = "super_admin", "Super Admin"
        ONBOARDING_AGENT = "onboarding_agent", "Onboarding Agent"
        CLINIC_MANAGER = "clinic_manager", "Clinic Manager"
        DOCTOR = "doctor", "Doctor"
        NURSE = "nurse", "Nurse"
        BILLING = "billing", "Billing Staff"

    role = models.CharField(max_length=24, choices=Role.choices, default=Role.DOCTOR)
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.SET_NULL, related_name="users")
    phone = models.CharField(max_length=30, blank=True)

    @property
    def is_platform_staff(self):
        return self.role in {self.Role.SUPER_ADMIN, self.Role.ONBOARDING_AGENT}

    def __str__(self):
        return self.get_full_name() or self.username


class DoctorProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="doctor_profile")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="doctors")
    specialty = models.CharField(max_length=120, default="General Practitioner")
    qualifications = models.CharField(max_length=255, blank=True)
    registration_number = models.CharField(max_length=100, blank=True)
    consultation_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(0)])
    room_or_department = models.CharField(max_length=100, blank=True)
    color = models.CharField(max_length=7, default="#5b67d8")
    bio = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user__first_name", "user__last_name"]

    def __str__(self):
        return f"Dr. {self.user.get_full_name()} · {self.specialty}"


class DoctorAvailability(models.Model):
    """Recurring weekly clinic hours. Used as an operational reference for scheduling."""
    doctor = models.ForeignKey(DoctorProfile, on_delete=models.CASCADE, related_name="availability")
    weekday = models.PositiveSmallIntegerField(validators=[MinValueValidator(0), MaxValueValidator(6)])
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["weekday", "start_time"]
        constraints = [models.UniqueConstraint(fields=["doctor", "weekday", "start_time", "end_time"], name="unique_doctor_availability")]



class Patient(models.Model):
    class Gender(models.TextChoices):
        FEMALE = "female", "Female"
        MALE = "male", "Male"
        OTHER = "other", "Other"
        NOT_SAY = "na", "Prefer not to say"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="patients")
    mrn = models.CharField(max_length=24, default="", blank=True)
    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=10, choices=Gender.choices, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    blood_group = models.CharField(max_length=5, blank=True)
    address = models.TextField(blank=True)
    allergies = models.TextField(blank=True, help_text="Record known allergies; do not leave blank when reviewed.")
    chronic_conditions = models.TextField(blank=True)
    emergency_contact_name = models.CharField(max_length=120, blank=True)
    emergency_contact_phone = models.CharField(max_length=30, blank=True)
    primary_doctor = models.ForeignKey(DoctorProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="primary_patients")
    doctors = models.ManyToManyField(DoctorProfile, through="PatientAccess", related_name="patients")
    tags = models.CharField(max_length=255, blank=True, help_text="Comma-separated, e.g. Diabetes, VIP")
    notes = models.TextField(blank=True)
    consent_to_contact = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_patients")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["first_name", "last_name"]
        indexes = [models.Index(fields=["organization", "mrn"]), models.Index(fields=["organization", "phone"])]
        constraints = [models.UniqueConstraint(fields=["organization", "mrn"], name="unique_patient_mrn_per_org")]

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def age(self):
        if not self.date_of_birth:
            return None
        today = timezone.localdate()
        return today.year - self.date_of_birth.year - ((today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day))

    def save(self, *args, **kwargs):
        if not self.mrn:
            self.mrn = f"PT-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return self.full_name


class PatientAccess(models.Model):
    """Explicit doctor-level access inside a clinic tenant."""
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    doctor = models.ForeignKey(DoctorProfile, on_delete=models.CASCADE)
    granted_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    reason = models.CharField(max_length=180, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["patient", "doctor"], name="unique_patient_doctor_access")]


class Appointment(models.Model):
    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        CONFIRMED = "confirmed", "Confirmed"
        CHECKED_IN = "checked_in", "Checked in"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No show"

    class Kind(models.TextChoices):
        IN_PERSON = "in_person", "In person"
        ONLINE = "online", "Online"
        FOLLOW_UP = "follow_up", "Follow-up"
        EMERGENCY = "emergency", "Emergency"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="appointments")
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="appointments")
    doctor = models.ForeignKey(DoctorProfile, on_delete=models.PROTECT, related_name="appointments")
    starts_at = models.DateTimeField()
    duration_minutes = models.PositiveSmallIntegerField(default=30, validators=[MinValueValidator(5), MaxValueValidator(480)])
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.IN_PERSON)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CONFIRMED)
    reason = models.CharField(max_length=255, blank=True)
    room = models.CharField(max_length=80, blank=True)
    fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(0)])
    internal_notes = models.TextField(blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_appointments")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["starts_at"]
        indexes = [models.Index(fields=["organization", "starts_at"]), models.Index(fields=["doctor", "starts_at"]), models.Index(fields=["status"])]

    @property
    def ends_at(self):
        return self.starts_at + timedelta(minutes=self.duration_minutes)

    def __str__(self):
        return f"{self.patient} · {self.starts_at:%d %b %Y %H:%M}"


class MedicalRecord(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="medical_records")
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="medical_records")
    doctor = models.ForeignKey(DoctorProfile, on_delete=models.PROTECT, related_name="medical_records")
    appointment = models.ForeignKey(Appointment, null=True, blank=True, on_delete=models.SET_NULL, related_name="medical_records")
    visit_date = models.DateTimeField(default=timezone.now)
    chief_complaint = models.TextField(blank=True)
    symptoms = models.TextField(blank=True)
    diagnosis = models.TextField(blank=True)
    examination = models.TextField(blank=True)
    treatment_plan = models.TextField(blank=True)
    clinical_notes = models.TextField(blank=True)
    blood_pressure = models.CharField(max_length=30, blank=True)
    pulse = models.CharField(max_length=30, blank=True)
    temperature = models.CharField(max_length=30, blank=True)
    spo2 = models.CharField(max_length=30, blank=True)
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    height_cm = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    follow_up_date = models.DateField(null=True, blank=True)
    is_sensitive = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-visit_date"]
        indexes = [models.Index(fields=["organization", "visit_date"]), models.Index(fields=["patient", "visit_date"])]

    def __str__(self):
        return f"{self.patient} · {self.visit_date:%d %b %Y}"


class Prescription(models.Model):
    record = models.ForeignKey(MedicalRecord, on_delete=models.CASCADE, related_name="prescriptions")
    instructions = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class PrescriptionItem(models.Model):
    prescription = models.ForeignKey(Prescription, on_delete=models.CASCADE, related_name="items")
    medicine = models.CharField(max_length=180)
    dosage = models.CharField(max_length=100, blank=True)
    frequency = models.CharField(max_length=100, blank=True)
    duration = models.CharField(max_length=100, blank=True)
    route = models.CharField(max_length=60, blank=True)
    instructions = models.CharField(max_length=255, blank=True)


class FollowUp(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        DONE = "done", "Done"
        CANCELLED = "cancelled", "Cancelled"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="follow_ups")
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="follow_ups")
    doctor = models.ForeignKey(DoctorProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="follow_ups")
    title = models.CharField(max_length=180)
    due_date = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    priority = models.CharField(max_length=12, choices=[("low", "Low"), ("normal", "Normal"), ("high", "High")], default="normal")
    notes = models.TextField(blank=True)
    generated_from_record = models.ForeignKey(MedicalRecord, null=True, blank=True, on_delete=models.SET_NULL, related_name="generated_followups")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["due_date"]
        indexes = [models.Index(fields=["organization", "status", "due_date"])]

    @property
    def is_overdue(self):
        return self.status == self.Status.OPEN and self.due_date < timezone.localdate()


class QueueEntry(models.Model):
    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting"
        WITH_DOCTOR = "with_doctor", "With doctor"
        COMPLETED = "completed", "Completed"
        LEFT = "left", "Left clinic"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="queue_entries")
    appointment = models.OneToOneField(Appointment, null=True, blank=True, on_delete=models.SET_NULL, related_name="queue_entry")
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="queue_entries")
    doctor = models.ForeignKey(DoctorProfile, on_delete=models.CASCADE, related_name="queue_entries")
    queue_date = models.DateField(default=timezone.localdate)
    token_number = models.PositiveIntegerField()
    priority = models.CharField(max_length=12, choices=[("normal", "Normal"), ("urgent", "Urgent"), ("emergency", "Emergency")], default="normal")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.WAITING)
    checked_in_at = models.DateTimeField(default=timezone.now)
    called_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["queue_date", "priority", "token_number"]
        constraints = [models.UniqueConstraint(fields=["organization", "queue_date", "token_number"], name="unique_daily_queue_token")]


class Invoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        UNPAID = "unpaid", "Unpaid"
        PARTIAL = "partial", "Partially paid"
        PAID = "paid", "Paid"
        VOID = "void", "Void"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="invoices")
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="invoices")
    doctor = models.ForeignKey(DoctorProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices")
    appointment = models.ForeignKey(Appointment, null=True, blank=True, on_delete=models.SET_NULL)
    invoice_number = models.CharField(max_length=32, unique=True, blank=True)
    issue_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField(null=True, blank=True)
    description = models.CharField(max_length=255, blank=True)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(0)])
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(0)])
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(0)])
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(0)])
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.UNPAID)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            self.invoice_number = f"INV-{timezone.now():%y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        self.total = max(Decimal("0.00"), self.subtotal - self.discount + self.tax)
        if self.status != self.Status.VOID and self.status != self.Status.DRAFT:
            if self.amount_paid >= self.total and self.total > 0:
                self.status = self.Status.PAID
            elif self.amount_paid > 0:
                self.status = self.Status.PARTIAL
            else:
                self.status = self.Status.UNPAID
        super().save(*args, **kwargs)

    @property
    def balance(self):
        return max(Decimal("0.00"), self.total - self.amount_paid)


class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        UPI = "upi", "UPI"
        BANK = "bank", "Bank transfer"
        OTHER = "other", "Other"

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    method = models.CharField(max_length=12, choices=Method.choices, default=Method.UPI)
    reference = models.CharField(max_length=120, blank=True)
    paid_at = models.DateTimeField(default=timezone.now)
    received_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    notes = models.CharField(max_length=255, blank=True)


class AuditLog(models.Model):
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.CASCADE, related_name="audit_logs")
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    description = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=180)
    message = models.TextField(blank=True)
    url = models.CharField(max_length=255, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class PracticeExpense(models.Model):
    class Category(models.TextChoices):
        RENT = "rent", "Rent"
        SALARY = "salary", "Staff salaries"
        MEDICAL = "medical", "Medical supplies"
        EQUIPMENT = "equipment", "Equipment"
        UTILITIES = "utilities", "Utilities"
        MARKETING = "marketing", "Marketing"
        MAINTENANCE = "maintenance", "Maintenance"
        OTHER = "other", "Other"

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        UPI = "upi", "UPI"
        CARD = "card", "Card"
        BANK = "bank", "Bank transfer"
        OTHER = "other", "Other"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="expenses")
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    title = models.CharField(max_length=180)
    vendor = models.CharField(max_length=180, blank=True)
    expense_date = models.DateField(default=timezone.localdate)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    payment_method = models.CharField(max_length=12, choices=Method.choices, default=Method.UPI)
    reference = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-expense_date", "-created_at"]
        indexes = [models.Index(fields=["organization", "expense_date"])]

    def __str__(self):
        return f"{self.title} · {self.amount}"


class InventoryItem(models.Model):
    class Category(models.TextChoices):
        MEDICINE = "medicine", "Medicines"
        CONSUMABLE = "consumable", "Clinical consumables"
        EQUIPMENT = "equipment", "Equipment"
        STATIONERY = "stationery", "Stationery"
        OTHER = "other", "Other"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="inventory_items")
    name = models.CharField(max_length=180)
    sku = models.CharField(max_length=64, blank=True)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.CONSUMABLE)
    unit = models.CharField(max_length=30, default="units")
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(0)])
    reorder_level = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("5.00"), validators=[MinValueValidator(0)])
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(0)])
    expiry_date = models.DateField(null=True, blank=True)
    storage_location = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["organization", "sku"], condition=~models.Q(sku=""), name="unique_inventory_sku_per_org")]
        indexes = [models.Index(fields=["organization", "category", "is_active"])]

    def save(self, *args, **kwargs):
        self.sku = (self.sku or "").strip().upper()
        super().save(*args, **kwargs)

    @property
    def is_low_stock(self):
        return self.quantity <= self.reorder_level

    def __str__(self):
        return self.name


class InventoryMovement(models.Model):
    class MovementType(models.TextChoices):
        STOCK_IN = "stock_in", "Stock received"
        STOCK_OUT = "stock_out", "Stock used"
        ADJUSTMENT = "adjustment", "Stock correction"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="inventory_movements")
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="movements")
    movement_type = models.CharField(max_length=16, choices=MovementType.choices)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))], help_text="For corrections, this is the newly counted stock level.")
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.item.name} · {self.get_movement_type_display()}"
