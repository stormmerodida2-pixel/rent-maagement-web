from django.core.management.base import BaseCommand

from notifications.utils import send_payment_summaries


class Command(BaseCommand):
    help = "WhatsApp each landlord who was paid today a summary (by SMS until WhatsApp is set up)."

    def handle(self, *args, **options):
        counts = send_payment_summaries()
        self.stdout.write(f"Sent {counts['whatsapp']} by WhatsApp, {counts['sms']} by SMS; "
                          f"{counts['failed']} fell back to SMS after a WhatsApp error.")
