"""Tests for the TEMPORARY grandfathered health-options config (gear + the
three restored behaviors). See DailyParticipant.legacy_health_config and its
REMOVE-WHEN-UNUSED note: delete this file with the feature."""
import json

from django.test import Client, TestCase
from django.utils import timezone

from daily.auth import SESSION_DAILY_PARTICIPANT_ID
from daily.models import ChecklistVersion, DailyParticipant


def _make(**kwargs):
    # Already onboarded so the dashboard (not the onboarding flow) renders.
    kwargs.setdefault("onboarded_at", timezone.now())
    return DailyParticipant.objects.create(
        display_name="Tester", kind=DailyParticipant.KIND_EXTERNAL, **kwargs
    )


def _client_for(participant):
    c = Client()
    s = c.session
    s[SESSION_DAILY_PARTICIPANT_ID] = participant.id
    s.save()
    return c


ALL_ON = {"auto_bonus": True, "coach_note": True, "reset": True}


class ConfigGearRenderTests(TestCase):
    def test_grandfathered_user_sees_gear_and_valid_js_config(self):
        p = _make(beta=True, legacy_health_config=dict(ALL_ON))
        html = _client_for(p).get("/daily/checkin/").content.decode()
        self.assertIn('id="cfg-gear"', html)
        # LEGACY_CFG must be valid JS (json), not a Python dict repr.
        self.assertIn('var LEGACY_CFG = {', html)
        self.assertIn('"auto_bonus"', html)
        self.assertNotIn("LEGACY_CFG = {'", html)  # no single-quoted Python dict
        self.assertNotIn("True", html.split("var LEGACY_CFG")[1][:80])

    def test_native_beta_user_has_no_gear(self):
        p = _make(beta=True, legacy_health_config=None)
        html = _client_for(p).get("/daily/checkin/").content.decode()
        self.assertNotIn('id="cfg-gear"', html)
        self.assertIn("var LEGACY_CFG = null;", html)


class ConfigToggleEndpointTests(TestCase):
    def _post(self, client, key, value):
        return client.post(
            "/daily/health-config/",
            data=json.dumps({"key": key, "value": value}),
            content_type="application/json",
        )

    def test_toggle_updates_config(self):
        p = _make(beta=True, legacy_health_config=dict(ALL_ON))
        r = self._post(_client_for(p), "auto_bonus", False)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        p.refresh_from_db()
        self.assertEqual(p.legacy_health_config["auto_bonus"], False)
        # Other keys untouched.
        self.assertEqual(p.legacy_health_config["coach_note"], True)

    def test_native_user_cannot_toggle(self):
        p = _make(beta=True, legacy_health_config=None)
        r = self._post(_client_for(p), "auto_bonus", False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"], "not_grandfathered")
        p.refresh_from_db()
        self.assertIsNone(p.legacy_health_config)  # never re-grandfathered

    def test_non_beta_rejected(self):
        p = _make(beta=False, legacy_health_config=dict(ALL_ON))
        r = self._post(_client_for(p), "auto_bonus", False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"], "not_beta")

    def test_bad_key_rejected(self):
        p = _make(beta=True, legacy_health_config=dict(ALL_ON))
        r = self._post(_client_for(p), "delete_everything", True)
        self.assertEqual(r.status_code, 400)
        p.refresh_from_db()
        self.assertEqual(p.legacy_health_config, ALL_ON)  # unchanged

    def test_non_boolean_value_rejected(self):
        p = _make(beta=True, legacy_health_config=dict(ALL_ON))
        client = _client_for(p)
        for value in ("false", 0, None):
            with self.subTest(value=value):
                r = self._post(client, "reset", value)
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()["error"], "bad_value")
        p.refresh_from_db()
        self.assertIs(p.legacy_health_config["reset"], True)


class ResetControlVisibilityTests(TestCase):
    """The reset form is rendered when the list has drifted from baseline; the
    'reset' option shows/hides it."""

    def _drift_off_baseline(self, participant):
        # Replace the current version with a clearly non-baseline single item.
        participant.checklist_versions.update(is_current=False)
        ChecklistVersion.objects.create(
            participant=participant,
            questions=[{"key": "u_custom", "label": "Something custom"}],
            source=ChecklistVersion.SOURCE_AI_MUTATION,
            is_current=True,
        )

    def test_reset_hidden_when_option_off(self):
        p = _make(beta=True, legacy_health_config={"auto_bonus": True, "coach_note": True, "reset": False})
        self._drift_off_baseline(p)
        html = _client_for(p).get("/daily/checkin/").content.decode()
        self.assertIn('id="reset-baseline-form"', html)
        self.assertIn("reset-baseline is-hidden", html)  # present but hidden

    def test_reset_shown_when_option_on(self):
        p = _make(beta=True, legacy_health_config=dict(ALL_ON))
        self._drift_off_baseline(p)
        html = _client_for(p).get("/daily/checkin/").content.decode()
        self.assertIn('id="reset-baseline-form"', html)
        self.assertNotIn("reset-baseline is-hidden", html)  # visible
