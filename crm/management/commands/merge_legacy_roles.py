from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from crm.models import Organization, User


class Command(BaseCommand):
    help = "Merge old clinic roles and give legacy no-expiry workspaces a 14-day transition trial."

    @transaction.atomic
    def handle(self, *args, **options):
        updated_roles = User.objects.filter(role__in=["clinic_admin", "receptionist"]).update(role=User.Role.CLINIC_MANAGER)

        # Existing deployments created before subscriptions existed may have active/trial
        # workspaces with no expiry. Give them a one-time 14-day transition trial instead
        # of silently granting indefinite access. New workspaces receive their own duration.
        now = timezone.now()
        legacy_workspaces = Organization.objects.filter(
            status__in=[Organization.Status.TRIAL, Organization.Status.ACTIVE],
            access_expires_at__isnull=True,
        )
        updated_workspaces = legacy_workspaces.update(
            status=Organization.Status.TRIAL,
            subscription_kind=Organization.SubscriptionKind.TRIAL,
            access_started_at=now,
            access_expires_at=now + timedelta(days=14),
            suspension_reason="",
        )
        self.stdout.write(self.style.SUCCESS(
            f"Updated {updated_roles} legacy account role(s); granted 14-day transition trials to {updated_workspaces} legacy workspace(s)."
        ))
