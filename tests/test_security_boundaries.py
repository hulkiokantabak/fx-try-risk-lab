from __future__ import annotations

import re
import unittest
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from risklab.quality import DataQualityError, validate_feed, validate_series
from scripts import build_browser_data as builder


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Point:
    observed_at: datetime
    value: float


@dataclass(frozen=True)
class Entry:
    title: str
    published_at: datetime


class FakeResponse:
    def __init__(self, payload: bytes, *, url: str, content_length: str | None = None) -> None:
        self.payload = payload
        self.url = url
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        return None

    def geturl(self) -> str:
        return self.url

    def read(self, maximum: int = -1) -> bytes:
        return self.payload if maximum < 0 else self.payload[:maximum]


class RemoteTransportTests(unittest.TestCase):
    def test_remote_urls_are_https_and_allowlisted(self) -> None:
        builder.validate_remote_url("https://data-api.ecb.europa.eu/service/data/EXR/example")
        builder.validate_remote_url("https://files.tcmb.gov.tr/example.zip")
        for unsafe in (
            "http://www.tcmb.gov.tr/example.zip",
            "https://example.net/example.zip",
            "https://user:password@www.tcmb.gov.tr/example.zip",
            "https://www.tcmb.gov.tr:444/example.zip",
        ):
            with self.subTest(url=unsafe):
                with self.assertRaises(builder.UnsafeRemoteDataError):
                    builder.validate_remote_url(unsafe)

    def test_bounded_reader_rejects_declared_and_streamed_oversize(self) -> None:
        with self.assertRaisesRegex(builder.UnsafeRemoteDataError, "declares"):
            builder.read_bounded_response(
                FakeResponse(b"small", url="https://www.tcmb.gov.tr", content_length="101"),
                maximum_bytes=100,
                label="fixture",
            )
        with self.assertRaisesRegex(builder.UnsafeRemoteDataError, "exceeds"):
            builder.read_bounded_response(
                FakeResponse(b"x" * 101, url="https://www.tcmb.gov.tr"),
                maximum_bytes=100,
                label="fixture",
            )

    def test_final_redirect_target_is_checked(self) -> None:
        response = FakeResponse(b"payload", url="https://attacker.example/payload")
        with patch.object(builder.REMOTE_OPENER, "open", return_value=response):
            with self.assertRaises(builder.UnsafeRemoteDataError):
                builder.fetch_text("https://www.tcmb.gov.tr/source")

    def test_cbrt_download_link_cannot_escape_allowlist(self) -> None:
        markup = '<a href="https://attacker.example/reserves.zip">zip link</a>'
        with self.assertRaises(builder.UnsafeRemoteDataError):
            builder.extract_cbrt_irfcl_zip_url(markup, "https://www.tcmb.gov.tr/page")


class ParserBoundaryTests(unittest.TestCase):
    def test_xml_dtd_and_entities_are_rejected(self) -> None:
        xml = '<!DOCTYPE x [<!ENTITY y "expanded">]><x>&y;</x>'
        with self.assertRaisesRegex(builder.UnsafeRemoteDataError, "DTD or entity"):
            builder.parse_xml_document(xml, label="fixture")

    def test_zip_bomb_ratio_and_unsafe_member_path_are_rejected(self) -> None:
        compressed = BytesIO()
        with zipfile.ZipFile(compressed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("large.xml", b"0" * 1_000_000)
        with zipfile.ZipFile(BytesIO(compressed.getvalue())) as archive:
            with self.assertRaisesRegex(builder.UnsafeRemoteDataError, "compression-ratio"):
                builder.validate_zip_archive(
                    archive,
                    label="fixture",
                    maximum_entries=10,
                    maximum_member_bytes=2_000_000,
                    maximum_total_bytes=2_000_000,
                )

        traversal = BytesIO()
        with zipfile.ZipFile(traversal, "w") as archive:
            archive.writestr("../sheet.xml", b"safe-sized")
        with zipfile.ZipFile(BytesIO(traversal.getvalue())) as archive:
            with self.assertRaisesRegex(builder.UnsafeRemoteDataError, "unsafe member path"):
                builder.validate_zip_archive(
                    archive,
                    label="fixture",
                    maximum_entries=10,
                    maximum_member_bytes=1_000,
                    maximum_total_bytes=1_000,
                )

    def test_zip_entry_count_is_bounded_before_zipfile_parses_members(self) -> None:
        payload = BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("one.txt", b"1")
            archive.writestr("two.txt", b"2")
        builder.preflight_zip_bytes(
            payload.getvalue(),
            label="fixture",
            maximum_bytes=10_000,
            maximum_entries=2,
        )
        with self.assertRaisesRegex(builder.UnsafeRemoteDataError, "too many entries"):
            builder.preflight_zip_bytes(
                payload.getvalue(),
                label="fixture",
                maximum_bytes=10_000,
                maximum_entries=1,
            )
        with self.assertRaisesRegex(builder.UnsafeRemoteDataError, "trailing data"):
            builder.preflight_zip_bytes(
                payload.getvalue() + b"polyglot",
                label="fixture",
                maximum_bytes=10_000,
                maximum_entries=2,
            )

    def test_missing_or_invalid_feed_date_is_not_fabricated_as_now(self) -> None:
        self.assertIsNone(builder.parse_feed_datetime(None))
        self.assertIsNone(builder.parse_feed_datetime("not a date"))

    def test_tampered_cache_payload_is_never_used_as_fallback(self) -> None:
        cache = {
            "sources": {
                "fixture": {
                    "payload": [{"observed_at": "2026-07-10T00:00:00", "value": 40.0}],
                    "checksum_sha256": "0" * 64,
                    "status": "fresh",
                }
            }
        }
        warnings: list[str] = []
        result = builder.try_fetch(
            "Fixture",
            lambda: (_ for _ in ()).throw(RuntimeError("network failure")),
            [],
            warnings,
            cache,
            "fixture",
            lambda value: value,
            lambda value: value,
        )
        self.assertEqual(result, [])
        self.assertEqual(cache["sources"]["fixture"]["status"], "unavailable")
        self.assertIn("checksum does not match", warnings[0])


class SemanticPoisoningTests(unittest.TestCase):
    def test_future_dated_series_and_feed_entries_are_rejected(self) -> None:
        future = datetime.now(UTC) + timedelta(days=10)
        with self.assertRaisesRegex(DataQualityError, "future"):
            validate_series([Point(future, 1.0)], minimum_count=1, positive=True)
        with self.assertRaisesRegex(DataQualityError, "future"):
            validate_feed([Entry("Future item", future)])


class SupplyChainTests(unittest.TestCase):
    def test_every_external_action_is_pinned_to_a_full_commit(self) -> None:
        workflow_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        )
        references = re.findall(r"^\s*uses:\s+([^\s]+)", workflow_text, flags=re.MULTILINE)
        self.assertTrue(references)
        for reference in references:
            with self.subTest(reference=reference):
                self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$")

    def test_static_site_has_no_third_party_runtime_script(self) -> None:
        for name in ("index.html", "methodology.html"):
            html = (ROOT / "docs" / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertNotIn("gc.zgo.at", html)
                self.assertNotIn("goatcounter", html.casefold())
                self.assertIn("script-src 'self'", html)
                self.assertIn("connect-src 'self'", html)


if __name__ == "__main__":
    unittest.main()
