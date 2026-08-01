from datetime import date
from types import SimpleNamespace

from django.test import SimpleTestCase

from rollcall.management.commands import post_rankings_to_telegram, post_rankings_to_x


class PublicationUrlTests(SimpleTestCase):
    def setUp(self):
        self.roll_call = SimpleNamespace(
            week_start_date=date(2026, 7, 20),
            week_end_date=date(2026, 7, 26),
            substack_url="https://strongasan0x.substack.com/p/week-of-2026-07-20",
        )

    def test_x_uses_the_canonical_on_site_url_for_legacy_rows(self):
        self.assertEqual(
            post_rankings_to_x._roll_call_url(self.roll_call),
            "https://strongasan0x.com/roll-call/2026-07-26/",
        )

    def test_telegram_uses_the_canonical_on_site_url_for_legacy_rows(self):
        self.assertEqual(
            post_rankings_to_telegram._roll_call_url(self.roll_call),
            "https://strongasan0x.com/roll-call/2026-07-26/",
        )

    def test_telegram_caption_escapes_warrior_names_and_labels_the_link(self):
        caption = post_rankings_to_telegram.build_caption(
            self.roll_call,
            [(1, "A < B", None)],
        )

        self.assertIn("A &lt; B", caption)
        self.assertIn(
            '<a href="https://strongasan0x.com/roll-call/2026-07-26/">Roll Call</a>',
            caption,
        )
        self.assertNotIn("substack.com", caption)
