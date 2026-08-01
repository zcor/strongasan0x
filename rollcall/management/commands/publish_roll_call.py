"""Retired one-step Roll Call publisher.

The original command targeted Substack and skipped the current preview,
website, and syndication checks. Keep the command name only to give operators
a safe, actionable error instead of silently running the obsolete flow.
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Retired. Use the roll-call-prep and roll-call-publish workflows."

    def handle(self, *args, **options):
        raise CommandError(
            "publish_roll_call is retired. Stage the on-site post with "
            "roll-call-prep, then publish with roll-call-publish."
        )
