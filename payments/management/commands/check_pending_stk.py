from django.core.management.base import BaseCommand

from payments import mpesa


class Command(BaseCommand):
    help = (
        'Ask Safaricom about STK prompts still pending after a couple of minutes (the callback may have '
        'been lost) and record the outcome. Run every 5 minutes from cron.'
    )

    def handle(self, *args, **options):
        summary = mpesa.check_pending_stk()
        self.stdout.write(self.style.SUCCESS(
            'Checked {checked}: {paid} paid, {failed} failed, {still_pending} still pending, {errors} errors.'.format(**summary)
        ))
