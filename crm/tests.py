from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from .models import Organization, User, DoctorProfile, Patient, PatientAccess, Appointment, InventoryItem, InventoryMovement
from .permissions import visible_patients, visible_records


class TenantAndDoctorIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.clinic = Organization.objects.create(name="Clinic One", slug="clinic-one", status=Organization.Status.ACTIVE)
        cls.other_clinic = Organization.objects.create(name="Clinic Two", slug="clinic-two", status=Organization.Status.ACTIVE)
        cls.admin = User.objects.create_user(username="clinicadmin", password="long-safe-password", role=User.Role.CLINIC_MANAGER, organization=cls.clinic)
        cls.doctor_a_user = User.objects.create_user(username="doctor-a", password="long-safe-password", role=User.Role.DOCTOR, organization=cls.clinic)
        cls.doctor_b_user = User.objects.create_user(username="doctor-b", password="long-safe-password", role=User.Role.DOCTOR, organization=cls.clinic)
        cls.other_admin = User.objects.create_user(username="other-admin", password="long-safe-password", role=User.Role.CLINIC_MANAGER, organization=cls.other_clinic)
        cls.manager = User.objects.create_user(username="frontdesk", password="long-safe-password", role=User.Role.CLINIC_MANAGER, organization=cls.clinic)
        cls.billing = User.objects.create_user(username="billing-user", password="long-safe-password", role=User.Role.BILLING, organization=cls.clinic)
        cls.nurse = User.objects.create_user(username="nurse-user", password="long-safe-password", role=User.Role.NURSE, organization=cls.clinic)
        cls.doctor_a = DoctorProfile.objects.create(user=cls.doctor_a_user, organization=cls.clinic, specialty="Cardiology")
        cls.doctor_b = DoctorProfile.objects.create(user=cls.doctor_b_user, organization=cls.clinic, specialty="Dermatology")
        cls.patient_a = Patient.objects.create(organization=cls.clinic, first_name="Patient", last_name="Alpha", primary_doctor=cls.doctor_a)
        cls.patient_b = Patient.objects.create(organization=cls.clinic, first_name="Patient", last_name="Beta", primary_doctor=cls.doctor_b)
        cls.patient_other = Patient.objects.create(organization=cls.other_clinic, first_name="Patient", last_name="Other")
        PatientAccess.objects.create(patient=cls.patient_a, doctor=cls.doctor_a, granted_by=cls.admin)
        PatientAccess.objects.create(patient=cls.patient_b, doctor=cls.doctor_b, granted_by=cls.admin)

    def test_doctor_patient_lists_are_independent(self):
        visible_a = visible_patients(self.doctor_a_user)
        visible_b = visible_patients(self.doctor_b_user)
        self.assertEqual(list(visible_a), [self.patient_a])
        self.assertEqual(list(visible_b), [self.patient_b])

    def test_doctor_cannot_open_another_doctors_patient(self):
        client = Client()
        client.force_login(self.doctor_a_user)
        response = client.get(reverse("patient_detail", args=[self.patient_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_clinic_user_cannot_open_another_tenants_patient(self):
        client = Client()
        client.force_login(self.admin)
        response = client.get(reverse("patient_detail", args=[self.patient_other.pk]))
        self.assertEqual(response.status_code, 404)

    def test_appointment_form_rejects_cross_doctor_overlap(self):
        start = timezone.now() + timedelta(days=1)
        Appointment.objects.create(
            organization=self.clinic, patient=self.patient_a, doctor=self.doctor_a,
            starts_at=start, duration_minutes=45, status=Appointment.Status.CONFIRMED,
        )
        from .forms import AppointmentForm
        form = AppointmentForm(data={
            "patient": self.patient_b.pk,
            "doctor": self.doctor_a.pk,
            "starts_at": timezone.localtime(start + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M"),
            "duration_minutes": 30, "kind": Appointment.Kind.IN_PERSON,
            "status": Appointment.Status.CONFIRMED, "fee": "0.00",
            "reason": "Overlap test",
        })
        form.fields["patient"].queryset = Patient.objects.filter(organization=self.clinic)
        form.fields["doctor"].queryset = DoctorProfile.objects.filter(organization=self.clinic)
        self.assertFalse(form.is_valid())
        self.assertIn("starts_at", form.errors)


    def test_disabled_user_session_is_revoked_immediately(self):
        client = Client()
        client.force_login(self.manager)
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"])
        response = client.get(reverse("patients"))
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)

    def test_suspended_workspace_revokes_existing_session(self):
        client = Client()
        client.force_login(self.admin)
        self.clinic.status = Organization.Status.SUSPENDED
        self.clinic.save(update_fields=["status"])
        response = client.get(reverse("patients"))
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)

    def test_inactive_doctor_profile_revokes_existing_session(self):
        client = Client()
        client.force_login(self.doctor_a_user)
        self.doctor_a.is_active = False
        self.doctor_a.save(update_fields=["is_active"])
        response = client.get(reverse("patients"))
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)

    def test_doctor_without_active_profile_cannot_log_in(self):
        orphan_doctor = User.objects.create_user(
            username="orphan-doctor", password="long-safe-password",
            role=User.Role.DOCTOR, organization=self.clinic,
        )
        client = Client()
        response = client.post(reverse("login"), {"username": orphan_doctor.username, "password": "long-safe-password"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", client.session)

    def test_patient_detail_has_no_media_upload_controls(self):
        client = Client()
        client.force_login(self.manager)
        response = client.get(reverse("patient_detail", args=[self.patient_a.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'type="file"')
        self.assertNotContains(response, "patient_document_upload")


    def test_application_rejects_multipart_file_uploads_globally(self):
        response = Client().post(
            reverse("login"),
            data="--boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"test.txt\"\r\nContent-Type: text/plain\r\n\r\nnot allowed\r\n--boundary--\r\n",
            content_type="multipart/form-data; boundary=boundary",
        )
        self.assertEqual(response.status_code, 415)
        self.assertContains(response, "File and media uploads are disabled", status_code=415)

    def test_patient_import_uses_pasted_csv_instead_of_file_upload(self):
        client = Client()
        client.force_login(self.manager)
        response = client.get(reverse("patient_import"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="csv_text"')
        self.assertNotContains(response, 'type="file"')

    def test_clinic_manager_patient_page_can_view_clinical_snapshot(self):
        self.patient_a.allergies = "Private allergy marker"
        self.patient_a.save(update_fields=["allergies"])
        Appointment.objects.create(
            organization=self.clinic, patient=self.patient_a, doctor=self.doctor_a,
            starts_at=timezone.now() + timedelta(days=2), reason="Private reason marker",
        )
        client = Client()
        client.force_login(self.manager)
        response = client.get(reverse("patient_detail", args=[self.patient_a.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Private allergy marker")
        self.assertContains(response, "Private reason marker")

    def test_clinic_manager_patient_form_exposes_full_patient_fields(self):
        client = Client()
        client.force_login(self.manager)
        response = client.get(reverse("patient_edit", args=[self.patient_a.pk]))
        self.assertEqual(response.status_code, 200)
        for field in ("blood_group", "allergies", "chronic_conditions", "emergency_contact_name", "emergency_contact_phone", "notes"):
            self.assertIn(field, response.context["form"].fields)

    def test_nurse_cannot_read_sensitive_medical_record(self):
        from .models import MedicalRecord
        sensitive = MedicalRecord.objects.create(
            organization=self.clinic, patient=self.patient_a, doctor=self.doctor_a,
            chief_complaint="Private note marker", is_sensitive=True,
        )
        self.assertFalse(visible_records(self.nurse).filter(pk=sensitive.pk).exists())

    def test_onboarding_agent_cannot_activate_a_workspace(self):
        agent = User.objects.create_user(username="agent", password="long-safe-password", role=User.Role.ONBOARDING_AGENT)
        org = Organization.objects.create(name="Agent Trial", slug="agent-trial", created_by=agent)
        client = Client()
        client.force_login(agent)
        response = client.post(reverse("organization_status", args=[org.pk]), {"status": Organization.Status.ACTIVE})
        self.assertEqual(response.status_code, 403)
        org.refresh_from_db()
        self.assertEqual(org.status, Organization.Status.TRIAL)

    def test_clinic_manager_can_disable_a_team_account(self):
        client = Client()
        client.force_login(self.admin)
        response = client.post(reverse("staff_status", args=[self.doctor_b_user.pk]), {"is_active": "0"})
        self.assertEqual(response.status_code, 302)
        self.doctor_b_user.refresh_from_db()
        self.assertFalse(self.doctor_b_user.is_active)

    def test_clinic_manager_cannot_disable_themselves(self):
        client = Client()
        client.force_login(self.admin)
        response = client.post(reverse("staff_status", args=[self.admin.pk]), {"is_active": "0"})
        self.assertEqual(response.status_code, 403)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)


class PracticeOperationsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Operations Clinic", slug="operations-clinic", status=Organization.Status.ACTIVE)
        self.admin = User.objects.create_user(username="ops-admin", password="long-safe-password", role=User.Role.CLINIC_MANAGER, organization=self.org)
        self.doctor = User.objects.create_user(username="ops-doctor", password="long-safe-password", role=User.Role.DOCTOR, organization=self.org)
        self.item = InventoryItem.objects.create(organization=self.org, name="Gloves", sku="GLV-001", quantity=Decimal("10"), reorder_level=Decimal("3"))

    def test_stock_movement_updates_balance_and_creates_history(self):
        client = Client()
        client.force_login(self.admin)
        response = client.post(reverse("inventory_movement", args=[self.item.pk]), {
            "movement_type": "stock_out", "quantity": "2", "note": "Demo usage",
        })
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, Decimal("8.00"))
        movement = InventoryMovement.objects.get(item=self.item)
        self.assertEqual(movement.balance_after, Decimal("8.00"))

    def test_clinic_doctor_cannot_access_clinic_expense_ledger(self):
        client = Client()
        client.force_login(self.doctor)
        response = client.get(reverse("expenses"))
        self.assertEqual(response.status_code, 403)

    def test_whatsapp_support_link_is_available_on_login_page(self):
        response = Client().get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "wa.me/919717324135")
        self.assertContains(response, "tel:+919717324135")


class SubscriptionLifecycleTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Subscription Clinic", slug="subscription-clinic",
            status=Organization.Status.TRIAL,
            subscription_kind=Organization.SubscriptionKind.TRIAL,
        )
        self.super_admin = User.objects.create_superuser(
            username="subscription-admin", email="sa@example.test", password="long-safe-password"
        )

    def test_trial_expiry_suspends_workspace(self):
        self.org.grant_access(
            Organization.SubscriptionKind.TRIAL, 1,
            starts_at=timezone.now() - timedelta(days=2),
        )
        self.assertTrue(self.org.expire_if_needed())
        self.org.refresh_from_db()
        self.assertEqual(self.org.status, Organization.Status.SUSPENDED)
        self.assertIn("Trial expired", self.org.suspension_reason)
        self.assertFalse(self.org.expire_if_needed())

    def test_paid_license_duration_is_selected_by_super_admin(self):
        client = Client()
        client.force_login(self.super_admin)
        response = client.post(reverse("organization_subscription", args=[self.org.pk]), {
            "subscription_kind": Organization.SubscriptionKind.LICENSE,
            "duration_days": "90",
        })
        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.status, Organization.Status.ACTIVE)
        self.assertEqual(self.org.subscription_kind, Organization.SubscriptionKind.LICENSE)
        self.assertAlmostEqual((self.org.access_expires_at - self.org.access_started_at).total_seconds(), 90 * 86400, delta=2)

    def test_onboarding_agent_cannot_grant_paid_license(self):
        agent = User.objects.create_user(username="subscription-agent", password="long-safe-password", role=User.Role.ONBOARDING_AGENT)
        client = Client()
        client.force_login(agent)
        response = client.post(reverse("organization_subscription", args=[self.org.pk]), {
            "subscription_kind": Organization.SubscriptionKind.LICENSE,
            "duration_days": "365",
        })
        self.assertEqual(response.status_code, 403)

    def test_health_check_expires_overdue_workspaces(self):
        self.org.grant_access(
            Organization.SubscriptionKind.TRIAL, 1,
            starts_at=timezone.now() - timedelta(days=2),
        )
        response = Client().get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.org.refresh_from_db()
        self.assertEqual(self.org.status, Organization.Status.SUSPENDED)

    def test_legacy_roles_merge_to_clinic_manager(self):
        legacy_workspace = Organization.objects.create(name="Legacy workspace", slug="legacy-no-expiry", status=Organization.Status.ACTIVE)
        legacy_admin = User.objects.create_user(username="legacy-admin", password="long-safe-password")
        legacy_admin.role = "clinic_admin"
        legacy_admin.save(update_fields=["role"])
        legacy_reception = User.objects.create_user(username="legacy-reception", password="long-safe-password")
        legacy_reception.role = "receptionist"
        legacy_reception.save(update_fields=["role"])
        from django.core.management import call_command
        from io import StringIO
        call_command("merge_legacy_roles", stdout=StringIO())
        legacy_admin.refresh_from_db()
        legacy_reception.refresh_from_db()
        self.assertEqual(legacy_admin.role, User.Role.CLINIC_MANAGER)
        self.assertEqual(legacy_reception.role, User.Role.CLINIC_MANAGER)
        legacy_workspace.refresh_from_db()
        self.assertEqual(legacy_workspace.status, Organization.Status.TRIAL)
        self.assertAlmostEqual((legacy_workspace.access_expires_at - timezone.now()).total_seconds(), 14 * 86400, delta=5)
