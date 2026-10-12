from django.core.management.base import BaseCommand

from storage.monitor import run_forever


class Command(BaseCommand):
    help = "Continuously probe storage nodes (failure detection), auto-repair and watch the blockchain."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=5.0)

    def handle(self, *args, **opts):
        self.stdout.write(f"monitoring storage nodes every {opts['interval']}s")
        run_forever(opts["interval"])
