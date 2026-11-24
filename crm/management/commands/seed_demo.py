from datetime import timedelta, date
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from crm.models import (
    User, Organization, DoctorProfile, Patient, PatientAccess, Appointment,
    MedicalRecord, Prescription, PrescriptionItem, FollowUp, Invoice,
    Payment, QueueEntry, AuditLog, Notification, PracticeExpense, InventoryItem, InventoryMovement,
)


class Command(BaseCommand):
    help = "Create CareCovery demo workspaces, role accounts and sample healthcare workflow data."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo data is disabled outside development. Use fictional demo data only on an isolated local instance.")
        with transaction.atomic():
            super_admin, _ = User.objects.get_or_create(username="superadmin", defaults={
                "first_name": "CareCovery", "last_name": "Platform", "email": "superadmin@carecovery.demo",
                "role": User.Role.SUPER_ADMIN, "is_staff": True, "is_superuser": True,
            })
            super_admin.first_name = "CareCovery"
            super_admin.last_name = "Platform"
            super_admin.email = "superadmin@carecovery.demo"
            super_admin.role = User.Role.SUPER_ADMIN
            super_admin.is_staff = True
            super_admin.is_superuser = True
            super_admin.is_active = True
            super_admin.set_password("CareCovery@2026")
            super_admin.save()

            onboarding, _ = User.objects.get_or_create(username="onboarding", defaults={
                "first_name": "Maya", "last_name": "Shah", "email": "maya@carecovery.demo",
                "role": User.Role.ONBOARDING_AGENT,
            })
            onboarding.first_name = "Maya"
            onboarding.last_name = "Shah"
            onboarding.email = "maya@carecovery.demo"
            onboarding.role = User.Role.ONBOARDING_AGENT
            onboarding.is_active = True
            onboarding.set_password("Onboard@2026")
            onboarding.save()

            clinic, _ = Organization.objects.get_or_create(slug="carecovery-demo-clinic", defaults={
                "name": "Northstar Family Clinic", "account_type": Organization.AccountType.CLINIC,
                "status": Organization.Status.ACTIVE, "email": "hello@northstar.demo",
                "phone": "+91 98765 43210", "address": "South Delhi, New Delhi, India",
                "created_by": super_admin,
            })
            clinic.name = "Northstar Family Clinic"
            clinic.account_type = Organization.AccountType.CLINIC
            clinic.status = Organization.Status.ACTIVE
            clinic.email = "hello@northstar.demo"
            clinic.phone = "+91 98765 43210"
            clinic.address = "South Delhi, New Delhi, India"
            clinic.created_by = super_admin
            clinic.save()
            if not clinic.access_expires_at:
                clinic.grant_access(Organization.SubscriptionKind.LICENSE, 3650)

            individual, _ = Organization.objects.get_or_create(slug="dr-neha-practice", defaults={
                "name": "Dr. Neha Kapoor's Practice", "account_type": Organization.AccountType.INDIVIDUAL,
                "status": Organization.Status.ACTIVE, "email": "neha@carecovery.demo",
                "phone": "+91 98111 22334", "address": "Dwarka, New Delhi, India",
                "created_by": onboarding,
            })
            individual.name = "Dr. Neha Kapoor's Practice"
            individual.account_type = Organization.AccountType.INDIVIDUAL
            individual.status = Organization.Status.ACTIVE
            individual.email = "neha@carecovery.demo"
            individual.phone = "+91 98111 22334"
            individual.address = "Dwarka, New Delhi, India"
            individual.created_by = onboarding
            individual.save()
            if not individual.access_expires_at:
                individual.grant_access(Organization.SubscriptionKind.LICENSE, 3650)

            clinic_manager = self.user("clinicmanager", "Ananya", "Mehra", "clinicmanager@northstar.demo", User.Role.CLINIC_MANAGER, clinic, "ClinicManager@2026")
            # Migrate and deactivate the former duplicate front-desk demo login.
            User.objects.filter(username="reception").update(role=User.Role.CLINIC_MANAGER, is_active=False)
            doctor_one = self.user("doctor1", "Priya", "Sharma", "priya@northstar.demo", User.Role.DOCTOR, clinic, "DoctorOne@2026")
            doctor_two = self.user("doctor2", "Arjun", "Mehta", "arjun@northstar.demo", User.Role.DOCTOR, clinic, "DoctorTwo@2026")
            nurse = self.user("nurse", "Karan", "Singh", "nurse@northstar.demo", User.Role.NURSE, clinic, "NurseUser@2026")
            billing = self.user("billing", "Dev", "Malhotra", "billing@northstar.demo", User.Role.BILLING, clinic, "BillingUser@2026")
            solo_doctor = self.user("individualdoctor", "Neha", "Kapoor", "neha@carecovery.demo", User.Role.DOCTOR, individual, "NehaDoctor@2026")

            dr1 = self.doctor(doctor_one, clinic, "Cardiology", "MBBS, MD (Medicine)", "DMC-2016-0441", Decimal("900.00"), "Consultation 1")
            dr2 = self.doctor(doctor_two, clinic, "Dermatology", "MBBS, MD (Dermatology)", "DMC-2018-0872", Decimal("750.00"), "Consultation 2")
            solo_profile = self.doctor(solo_doctor, individual, "Family Medicine", "MBBS, DNB", "DMC-2015-0198", Decimal("650.00"), "Independent practice")

            patients = []
            patients.append(self.patient(clinic, "Aarav", "Khanna", "1991-02-14", "male", "+91 98990 10001", "aarav.khanna@example.demo", "B+", dr1, "Hypertension, Follow-up", "Penicillin allergy", "Hypertension"))
            patients.append(self.patient(clinic, "Sana", "Iqbal", "1986-08-09", "female", "+91 98990 10002", "sana.iqbal@example.demo", "O+", dr1, "Cardiac risk", "No known allergies", "Dyslipidaemia"))
            patients.append(self.patient(clinic, "Vihaan", "Reddy", "2000-11-23", "male", "+91 98990 10003", "vihaan.reddy@example.demo", "A+", dr2, "New patient", "No known allergies", ""))
            patients.append(self.patient(clinic, "Meera", "Bansal", "1994-05-30", "female", "+91 98990 10004", "meera.bansal@example.demo", "AB+", dr2, "Acne care", "No known allergies", ""))
            solo_patient = self.patient(individual, "Kabir", "Sethi", "1979-12-01", "male", "+91 98111 22335", "kabir.sethi@example.demo", "O-", solo_profile, "Annual review", "Sulfa allergy", "Type 2 diabetes")

            now = timezone.now()
            appt1 = self.appointment(clinic, patients[0], dr1, now + timedelta(minutes=35), "Blood pressure review", Decimal("900.00"), Appointment.Status.CONFIRMED, Appointment.Kind.FOLLOW_UP)
            appt2 = self.appointment(clinic, patients[1], dr1, now + timedelta(hours=2), "Palpitations consultation", Decimal("900.00"), Appointment.Status.CONFIRMED, Appointment.Kind.IN_PERSON)
            appt3 = self.appointment(clinic, patients[2], dr2, now + timedelta(hours=3), "Skin rash assessment", Decimal("750.00"), Appointment.Status.CHECKED_IN, Appointment.Kind.IN_PERSON)
            self.appointment(clinic, patients[3], dr2, now - timedelta(days=1), "Acne follow-up", Decimal("750.00"), Appointment.Status.COMPLETED, Appointment.Kind.FOLLOW_UP)
            self.appointment(individual, solo_patient, solo_profile, now + timedelta(days=1), "Routine follow-up", Decimal("650.00"), Appointment.Status.CONFIRMED, Appointment.Kind.FOLLOW_UP)

            record, _ = MedicalRecord.objects.get_or_create(
                organization=clinic, patient=patients[0], doctor=dr1,
                chief_complaint="Routine blood pressure review",
                defaults={
                    "appointment": None, "visit_date": now - timedelta(days=14),
                    "symptoms": "Occasional mild headache; no chest pain.",
                    "diagnosis": "Essential hypertension — review",
                    "examination": "Patient alert; no acute distress.",
                    "treatment_plan": "Continue prescribed therapy; monitor blood pressure at home.",
                    "clinical_notes": "Discussed salt reduction, hydration and daily readings.",
                    "blood_pressure": "136/86 mmHg", "pulse": "78 bpm", "temperature": "36.8 °C", "spo2": "98%",
                    "weight_kg": Decimal("74.50"), "height_cm": Decimal("174.00"),
                    "follow_up_date": timezone.localdate() + timedelta(days=10),
                },
            )
            prescription, _ = Prescription.objects.get_or_create(record=record, defaults={"instructions": "Take as prescribed. Review if dizziness or unusual symptoms occur."})
            PrescriptionItem.objects.get_or_create(prescription=prescription, medicine="Amlodipine", defaults={
                "dosage": "5 mg", "frequency": "Once daily", "duration": "30 days", "route": "Oral", "instructions": "At the same time each day",
            })
            FollowUp.objects.get_or_create(organization=clinic, patient=patients[0], title="Blood pressure review", defaults={
                "doctor": dr1, "due_date": timezone.localdate() + timedelta(days=10), "priority": "high",
                "notes": "Bring the home BP readings for review.", "generated_from_record": record, "created_by": clinic_manager,
            })
            FollowUp.objects.get_or_create(organization=clinic, patient=patients[1], title="Review lipid profile", defaults={
                "doctor": dr1, "due_date": timezone.localdate() - timedelta(days=2), "priority": "normal",
                "notes": "Reminder to review recent lab results.", "created_by": clinic_manager,
            })

            invoice = Invoice.objects.filter(organization=clinic, patient=patients[0], description="Consultation and review").first()
            if not invoice:
                invoice = Invoice.objects.create(
                    organization=clinic, patient=patients[0], doctor=dr1,
                    description="Consultation and review", subtotal=Decimal("900.00"), discount=Decimal("100.00"),
                    tax=Decimal("0.00"), issue_date=timezone.localdate() - timedelta(days=2),
                    due_date=timezone.localdate() + timedelta(days=5), created_by=clinic_manager,
                )
            if not invoice.payments.exists():
                payment = Payment.objects.create(invoice=invoice, amount=Decimal("400.00"), method=Payment.Method.UPI,
                                                 reference="DEMO-UPI-0400", received_by=billing, notes="Part payment — demo")
                invoice.amount_paid = payment.amount
                invoice.save()

            QueueEntry.objects.get_or_create(appointment=appt3, defaults={
                "organization": clinic, "patient": patients[2], "doctor": dr2,
                "queue_date": timezone.localdate(), "token_number": 1,
                "priority": "urgent", "status": QueueEntry.Status.WAITING,
            })

            # Practice operations demo data: expense ledger and stock controls.
            demo_expenses = [
                ("Clinic rent · November", PracticeExpense.Category.RENT, "Property management", Decimal("28000.00")),
                ("Disposable examination gloves", PracticeExpense.Category.MEDICAL, "MedSupply India", Decimal("2450.00")),
                ("Electricity and internet", PracticeExpense.Category.UTILITIES, "Utilities provider", Decimal("3900.00")),
            ]
            for title, category, vendor, amount in demo_expenses:
                PracticeExpense.objects.get_or_create(organization=clinic, title=title, expense_date=timezone.localdate().replace(day=1), defaults={
                    "category": category, "vendor": vendor, "amount": amount, "payment_method": PracticeExpense.Method.UPI,
                    "reference": "DEMO-EXPENSE", "created_by": clinic_manager,
                })

            demo_inventory = [
                ("Examination gloves · Medium", "SUP-GLOVE-M", InventoryItem.Category.CONSUMABLE, "boxes", Decimal("18"), Decimal("8"), Decimal("125"), None, "Store room"),
                ("Digital thermometer", "EQ-THERMO-01", InventoryItem.Category.EQUIPMENT, "units", Decimal("3"), Decimal("2"), Decimal("450"), None, "Nurse station"),
                ("Alcohol swabs", "SUP-SWAB-01", InventoryItem.Category.CONSUMABLE, "packs", Decimal("4"), Decimal("6"), Decimal("85"), None, "Treatment room"),
                ("Hand sanitiser 500ml", "SUP-SAN-500", InventoryItem.Category.CONSUMABLE, "bottles", Decimal("12"), Decimal("5"), Decimal("110"), None, "Reception"),
            ]
            for name, sku, category, unit, quantity, reorder, cost, expiry, location in demo_inventory:
                item, created = InventoryItem.objects.get_or_create(organization=clinic, sku=sku, defaults={
                    "name": name, "category": category, "unit": unit, "quantity": quantity,
                    "reorder_level": reorder, "unit_cost": cost, "expiry_date": expiry, "storage_location": location,
                })
                if created and quantity > 0:
                    InventoryMovement.objects.create(organization=clinic, item=item, movement_type=InventoryMovement.MovementType.STOCK_IN, quantity=quantity, balance_after=quantity, note="Demo opening stock", created_by=clinic_manager)

            PracticeExpense.objects.get_or_create(organization=individual, title="Practice internet and utilities", expense_date=timezone.localdate().replace(day=1), defaults={
                "category": PracticeExpense.Category.UTILITIES, "vendor": "Local utilities provider",
                "amount": Decimal("1850.00"), "payment_method": PracticeExpense.Method.UPI,
                "reference": "DEMO-SOLO-EXPENSE", "created_by": solo_doctor,
            })
            solo_stock, solo_stock_created = InventoryItem.objects.get_or_create(organization=individual, sku="SOLO-BP-CUFF", defaults={
                "name": "Digital blood pressure monitor", "category": InventoryItem.Category.EQUIPMENT,
                "unit": "units", "quantity": Decimal("2"), "reorder_level": Decimal("1"),
                "unit_cost": Decimal("1650.00"), "storage_location": "Consultation room",
            })
            if solo_stock_created:
                InventoryMovement.objects.create(organization=individual, item=solo_stock, movement_type=InventoryMovement.MovementType.STOCK_IN, quantity=solo_stock.quantity, balance_after=solo_stock.quantity, note="Demo opening stock", created_by=solo_doctor)

            AuditLog.objects.get_or_create(organization=clinic, action="demo.seeded", object_type="Organization", object_id=str(clinic.pk), defaults={
                "actor": super_admin, "description": "CareCovery demo clinic data was initialized.",
            })
            Notification.objects.get_or_create(user=doctor_one, title="Your demo workspace is ready", defaults={
                "message": "Review today's schedule, patient timeline and follow-up reminders.", "url": "/appointments/",
            })
            Notification.objects.get_or_create(user=clinic_manager, title="Welcome to CareCovery", defaults={
                "message": "Your clinic is ready. Invite your team and explore the demo workflows.", "url": "/team/",
            })

        self.stdout.write(self.style.SUCCESS("CareCovery demo data created/updated."))
        self.stdout.write("Demo logins (passwords are for local demo use only):")
        self.stdout.write("  Super Admin:       superadmin / CareCovery@2026")
        self.stdout.write("  Onboarding Agent:  onboarding / Onboard@2026")
        self.stdout.write("  Clinic Manager:    clinicmanager / ClinicManager@2026")
        self.stdout.write("  Doctor 1:          doctor1 / DoctorOne@2026")
        self.stdout.write("  Doctor 2:          doctor2 / DoctorTwo@2026")
        self.stdout.write("  Nurse:             nurse / NurseUser@2026")
        self.stdout.write("  Billing:           billing / BillingUser@2026")
        self.stdout.write("  Individual Doctor: individualdoctor / NehaDoctor@2026")

    def user(self, username, first, last, email, role, organization, password):
        user, _ = User.objects.get_or_create(username=username, defaults={
            "first_name": first, "last_name": last, "email": email,
            "role": role, "organization": organization,
        })
        user.first_name = first
        user.last_name = last
        user.email = email
        user.role = role
        user.organization = organization
        user.is_active = True
        user.set_password(password)
        user.save()
        return user

    def doctor(self, user, organization, specialty, qualifications, reg_no, fee, room):
        profile, _ = DoctorProfile.objects.get_or_create(user=user, defaults={"organization": organization})
        profile.organization = organization
        profile.specialty = specialty
        profile.qualifications = qualifications
        profile.registration_number = reg_no
        profile.consultation_fee = fee
        profile.room_or_department = room
        profile.is_active = True
        profile.save()
        return profile

    def patient(self, organization, first, last, dob, gender, phone, email, blood, doctor, tags, allergies, conditions):
        patient = Patient.objects.filter(organization=organization, phone=phone).first()
        if not patient:
            patient = Patient(organization=organization, first_name=first, last_name=last, phone=phone)
        patient.first_name = first
        patient.last_name = last
        patient.date_of_birth = date.fromisoformat(dob)
        patient.gender = gender
        patient.phone = phone
        patient.email = email
        patient.blood_group = blood
        patient.primary_doctor = doctor
        patient.tags = tags
        patient.allergies = allergies
        patient.chronic_conditions = conditions
        patient.created_by = doctor.user
        patient.save()
        PatientAccess.objects.get_or_create(patient=patient, doctor=doctor, defaults={"granted_by": doctor.user, "reason": "Primary doctor"})
        return patient

    def appointment(self, organization, patient, doctor, starts_at, reason, fee, status, kind):
        appt = Appointment.objects.filter(organization=organization, patient=patient, doctor=doctor, reason=reason).first()
        if not appt:
            appt = Appointment(organization=organization, patient=patient, doctor=doctor, reason=reason)
        appt.starts_at = starts_at
        appt.duration_minutes = 30
        appt.fee = fee
        appt.status = status
        appt.kind = kind
        appt.created_by = doctor.user
        appt.save()
        PatientAccess.objects.get_or_create(patient=patient, doctor=doctor, defaults={"granted_by": doctor.user, "reason": "Appointment assignment"})
        return appt
