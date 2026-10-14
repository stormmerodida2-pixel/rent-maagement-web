import signal
import time

from django.core.management.base import BaseCommand

from ops.utils import JOBS, run_due_jobs


class Command(BaseCommand):
    help = 'Run scheduled jobs (billing, reminders, M-Pesa checks, backups). Run exactly one of these.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Run whatever is due now, then exit.')
        parser.add_argument('--interval', type=int, default=30, help='Seconds between checks (default 30).')

    def handle(self, *args, **options):
        if options['once']:
            ran = run_due_jobs()
            self.stdout.write(f"Ran: {', '.join(ran) or 'nothing due'}")
            return

        stopping = False

        def stop(signum, frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.stdout.write(f"Scheduler started with {len(JOBS)} jobs: {', '.join(j.name for j in JOBS)}")
        while not stopping:
            run_due_jobs()
            for _ in range(options['interval']):
                if stopping:
                    break
                time.sleep(1)
        self.stdout.write('Scheduler stopped.')
