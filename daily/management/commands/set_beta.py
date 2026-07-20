"""Manage individual beta flags and reversible cohort rollouts.

Individual mode (kept for the existing allowlisted production workflow):

    python manage.py set_beta --name Amy --ai
    python manage.py set_beta --name Amy --off

Reversible rollout mode:

    python manage.py set_beta --rollout --dry-run
    python manage.py set_beta --rollout --participant 10 --participant 21
    python manage.py set_beta --status
    python manage.py set_beta --rollback <rollout-uuid>

Rollout targets are grandfathered participants that are still on legacy. Each
cohort stores its exact original beta/AI/focus values in the database before
any flag changes. Rollback restores those values without deleting activity
created while beta was active.
"""

import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from daily.models import DailyBetaRollout, DailyParticipant
from daily.services.checklist import dismiss_pending_mutations


class Command(BaseCommand):
    help = "Set one beta participant or apply/rollback a snapshotted beta cohort."

    def add_arguments(self, parser):
        parser.add_argument(
            "--name", help="Participant display_name for individual mode."
        )
        parser.add_argument(
            "--off", action="store_true", help="Individual mode: turn beta off."
        )
        parser.add_argument(
            "--ai",
            action="store_true",
            help="Individual mode: enable overnight AI checklist curation.",
        )
        parser.add_argument(
            "--rollout",
            action="store_true",
            help="Roll out all eligible legacy participants, or --participant IDs.",
        )
        parser.add_argument(
            "--participant",
            action="append",
            type=int,
            default=[],
            help="Rollout mode: target one participant id (repeatable).",
        )
        parser.add_argument(
            "--rollback", metavar="ROLLOUT_ID", help="Restore an applied cohort."
        )
        parser.add_argument(
            "--status", action="store_true", help="List recent rollout records."
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Report without saving changes."
        )

    def handle(self, *args, **opts):
        modes = sum(bool(value) for value in (
            opts.get("name"), opts["rollout"], opts.get("rollback"), opts["status"],
        ))
        if modes != 1:
            raise CommandError(
                "Choose exactly one mode: --name, --rollout, --rollback, or --status."
            )

        if opts["participant"] and not opts["rollout"]:
            raise CommandError("--participant is only valid with --rollout.")
        if (opts["off"] or opts["ai"]) and not opts.get("name"):
            raise CommandError("--off and --ai are only valid with --name.")
        if opts["dry_run"] and (opts.get("rollback") or opts["status"]):
            raise CommandError("--dry-run is only valid with --name or --rollout.")

        if opts.get("name"):
            return self._individual(opts)
        if opts["rollout"]:
            return self._rollout(opts)
        if opts.get("rollback"):
            return self._rollback(opts["rollback"])
        return self._status()

    def _individual(self, opts):
        name = opts["name"].strip()
        matches = list(DailyParticipant.objects.filter(display_name__iexact=name))
        if not matches:
            raise CommandError(f"No DailyParticipant with display_name '{name}'.")
        if len(matches) > 1:
            ids = ", ".join(str(participant.id) for participant in matches)
            raise CommandError(
                f"Ambiguous: {len(matches)} participants named '{name}' "
                f"(ids: {ids}). Refusing to guess."
            )

        participant = matches[0]
        want_beta = not opts["off"]
        want_ai = bool(opts["ai"]) and want_beta
        self.stdout.write(
            f"{participant.display_name} (id={participant.id}): "
            f"beta {participant.beta} -> {want_beta}, "
            f"ai_mutations_enabled {participant.ai_mutations_enabled} -> {want_ai}"
        )
        if opts["dry_run"]:
            self.stdout.write("dry-run: no changes saved.")
            return

        participant.beta = want_beta
        participant.ai_mutations_enabled = want_ai
        participant.save(update_fields=[
            "beta", "ai_mutations_enabled", "updated_at",
        ])
        if want_beta and not want_ai:
            dismiss_pending_mutations(participant)
        self.stdout.write(self.style.SUCCESS("saved."))

    def _eligible_rollout_targets(self, participant_ids):
        queryset = DailyParticipant.objects.filter(
            legacy_health_config__isnull=False,
            beta=False,
        ).order_by("id")
        if participant_ids:
            requested = set(participant_ids)
            queryset = queryset.filter(id__in=requested)
            found = set(queryset.values_list("id", flat=True))
            invalid = sorted(requested - found)
            if invalid:
                raise CommandError(
                    "Not eligible for rollout (missing, already beta, or not "
                    "grandfathered): " + ", ".join(map(str, invalid))
                )
        return queryset

    def _rollout(self, opts):
        targets = list(self._eligible_rollout_targets(opts["participant"]))
        if not targets:
            self.stdout.write("No eligible legacy participants remain.")
            return

        self.stdout.write(f"Eligible rollout cohort: {len(targets)} participant(s)")
        for participant in targets:
            focus = participant.focus or DailyParticipant.FOCUS_HEALTH
            self.stdout.write(
                f"  {participant.id}: {participant.display_name} | "
                f"beta {participant.beta}->True, "
                f"AI {participant.ai_mutations_enabled}->True, "
                f"focus {participant.focus!r}->{focus!r}"
            )
        if opts["dry_run"]:
            self.stdout.write("dry-run: no snapshot or participant flags saved.")
            return

        target_ids = [participant.id for participant in targets]
        with transaction.atomic():
            locked = list(
                DailyParticipant.objects.select_for_update()
                .filter(id__in=target_ids)
                .order_by("id")
            )
            if [participant.id for participant in locked] != target_ids:
                raise CommandError("Rollout cohort changed while locking; retry.")

            snapshot = [
                {
                    "id": participant.id,
                    "beta": participant.beta,
                    "ai_mutations_enabled": participant.ai_mutations_enabled,
                    "focus": participant.focus,
                }
                for participant in locked
            ]
            now = timezone.now()
            rollout = DailyBetaRollout.objects.create(
                snapshot=snapshot,
                target_count=len(locked),
                applied_at=now,
            )
            for participant in locked:
                participant.beta = True
                participant.ai_mutations_enabled = True
                participant.focus = (
                    participant.focus or DailyParticipant.FOCUS_HEALTH
                )
                participant.save(update_fields=[
                    "beta", "ai_mutations_enabled", "focus", "updated_at",
                ])

        self.stdout.write(self.style.SUCCESS(
            f"rollout applied: {rollout.rollout_id} ({rollout.target_count} participants)"
        ))
        self.stdout.write(
            f"rollback: python manage.py set_beta --rollback {rollout.rollout_id}"
        )

    def _rollback(self, raw_rollout_id):
        try:
            rollout_id = uuid.UUID(str(raw_rollout_id))
        except (TypeError, ValueError, AttributeError):
            raise CommandError(f"Invalid rollout id: {raw_rollout_id}")

        with transaction.atomic():
            try:
                rollout = DailyBetaRollout.objects.select_for_update().get(
                    rollout_id=rollout_id
                )
            except DailyBetaRollout.DoesNotExist:
                raise CommandError(f"Unknown rollout id: {rollout_id}")
            if rollout.status != DailyBetaRollout.STATUS_APPLIED:
                raise CommandError(
                    f"Rollout {rollout_id} is already {rollout.status}."
                )

            snapshot = rollout.snapshot or []
            original_by_id = {int(item["id"]): item for item in snapshot}
            participants = {
                participant.id: participant
                for participant in DailyParticipant.objects.select_for_update().filter(
                    id__in=original_by_id
                )
            }
            missing = sorted(set(original_by_id) - set(participants))
            if missing:
                raise CommandError(
                    "Cannot safely rollback; participants missing: "
                    + ", ".join(map(str, missing))
                )

            for participant_id, original in original_by_id.items():
                participant = participants[participant_id]
                participant.beta = bool(original["beta"])
                participant.ai_mutations_enabled = bool(
                    original["ai_mutations_enabled"]
                )
                participant.focus = str(original["focus"])
                participant.save(update_fields=[
                    "beta", "ai_mutations_enabled", "focus", "updated_at",
                ])

            rollout.status = DailyBetaRollout.STATUS_ROLLED_BACK
            rollout.rolled_back_at = timezone.now()
            rollout.save(update_fields=["status", "rolled_back_at"])

        self.stdout.write(self.style.SUCCESS(
            f"rollback complete: {rollout_id} ({rollout.target_count} participants)"
        ))

    def _status(self):
        rollouts = list(DailyBetaRollout.objects.order_by("-created_at")[:10])
        if not rollouts:
            self.stdout.write("No beta rollout records.")
            return
        for rollout in rollouts:
            self.stdout.write(
                f"{rollout.rollout_id} | {rollout.status} | "
                f"targets={rollout.target_count} | "
                f"applied={rollout.applied_at.isoformat()}"
            )
