from django.core.management.base import BaseCommand

from crm.models import Organization


class Command(BaseCommand):
    help = "Suspend trials and paid licenses that have passed their expiry date."

    def handle(self, *args, **options):
        count = Organization.expire_overdue()
        self.stdout.write(self.style.SUCCESS(f"Suspended {count} expired workspace(s)."))
