from __future__ import annotations

import unittest
from datetime import datetime

from scripts.build_browser_data import (
    MODEL_VERSION,
    build_news_section,
    build_score_history_chart,
    deserialize_series,
    parse_irfcl_header_date,
    try_fetch,
)


class ContextAvailabilityTests(unittest.TestCase):
    def test_unavailable_news_feed_is_not_converted_to_zero_evidence(self) -> None:
        news = build_news_section(
            [],
            [],
            headline_available=False,
            chatter_available=False,
        )

        self.assertIsNone(news["headline_count_14d"])
        self.assertIsNone(news["chatter_count_14d"])
        self.assertIsNone(news["score"])

    def test_invalid_cache_emits_one_warning_and_is_unavailable(self) -> None:
        cache = {
            "sources": {
                "ecb_eurtry": {
                    "payload": [{"observed_at": "2026-07-10T00:00:00", "value": 40.0}],
                    "status": "fresh",
                }
            }
        }
        warnings: list[str] = []

        result = try_fetch(
            "ECB EUR/TRY",
            lambda: (_ for _ in ()).throw(RuntimeError("network failure")),
            [],
            warnings,
            cache,
            "ecb_eurtry",
            lambda value: value,
            deserialize_series,
        )

        self.assertEqual(result, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("cached fallback also failed", warnings[0])
        self.assertEqual(cache["sources"]["ecb_eurtry"]["status"], "unavailable")


class PublicationHistoryTests(unittest.TestCase):
    def test_history_chart_excludes_legacy_and_other_model_versions(self) -> None:
        chart = build_score_history_chart(
            [
                {"as_of": "2026-07-09T00:00:00Z", "primary_score": 40.0},
                {
                    "as_of": "2026-07-10T00:00:00Z",
                    "primary_score": 30.0,
                    "model_version": "other-model",
                    "forecast_id": "other",
                },
                {
                    "as_of": "2026-07-11T00:00:00Z",
                    "primary_score": 9.2,
                    "model_version": MODEL_VERSION,
                    "forecast_id": "current",
                },
            ]
        )

        self.assertEqual(chart["series"][0]["points"], [
            {"date": "2026-07-11", "value": 9.2, "stance": "n/a"}
        ])

    def test_reserve_header_rejects_implausible_excel_serial(self) -> None:
        self.assertIsNone(parse_irfcl_header_date("159694"))
        self.assertEqual(parse_irfcl_header_date("2026-07-03"), datetime(2026, 7, 3))


if __name__ == "__main__":
    unittest.main()
