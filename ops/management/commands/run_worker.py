import logging
import time

from django.core.management.base import BaseCommand

from ops.outbox import process_batch


class Command(BaseCommand):
    help = "Process outbox jobs (notifications) with retries."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process due jobs once and exit.")
        parser.add_argument("--sleep", type=float, default=2.0, help="Seconds to wait when idle.")

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        self.stdout.write("Outbox worker started.")
        while True:
            counts = process_batch()
            if any(counts.values()):
                self.stdout.write(f"Processed: {counts}")
            if options["once"]:
                break
            if not any(counts.values()):
                time.sleep(options["sleep"])
