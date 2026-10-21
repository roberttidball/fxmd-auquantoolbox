"""Focused calendar adapter regressions, independent of the legacy backtester."""

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError


def load_adapter():
    data_source = types.ModuleType("backtester.dataSource.data_source")
    data_source.DataSource = object
    updates = types.ModuleType("backtester.instrumentUpdates")
    updates.StockInstrumentUpdate = lambda **kwargs: types.SimpleNamespace(**kwargs)
    spec = importlib.util.spec_from_file_location(
        "calendar_adapter",
        Path(__file__).resolve().parents[1]
        / "backtester/dataSource/fxmacrodata_data_source.py",
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "backtester.dataSource.data_source": data_source,
            "backtester.instrumentUpdates": updates,
        },
    ):
        spec.loader.exec_module(module)
    return module


adapter = load_adapter()


class CalendarTests(unittest.TestCase):
    def test_currency_stays_in_one_request_path_segment(self):
        for currency in ("usd/extra", "usd?api_key=other", "usd#fragment", "../outside", "usd%2Fextra"):
            with self.subTest(currency=currency), tempfile.TemporaryDirectory() as directory:
                source = adapter.FXMacroDataCalendarDataSource.__new__(adapter.FXMacroDataCalendarDataSource)
                source._currency = currency
                source._startDate = source._endDate = datetime(2026, 10, 20)
                source._topTierOnly = False
                response = unittest.mock.MagicMock()
                response.__enter__.return_value.read.return_value = b'{"data": []}'
                with patch.object(adapter, "urlopen", return_value=response) as request, patch.dict("os.environ", {"FXMD_API_KEY": ""}):
                    source.downloadAndAdjustData("macro_calendar", str(Path(directory) / "calendar.json"))
                url = request.call_args.args[0].full_url
                from urllib.parse import quote, urlsplit
                parsed = urlsplit(url)
                self.assertEqual(parsed.path, "/v1/calendar/" + quote(currency, safe=""))
                self.assertEqual(parsed.query, "start_date=2026-10-20&end_date=2026-10-20")
                self.assertEqual(parsed.fragment, "")

    def test_currency_cannot_escape_calendar_cache_directory(self):
        for currency in ("../outside", "../../outside", "usd/nested", "usd\\nested"):
            with self.subTest(currency=currency), tempfile.TemporaryDirectory() as directory:
                source = adapter.FXMacroDataCalendarDataSource.__new__(adapter.FXMacroDataCalendarDataSource)
                source._currency = currency
                source._startDate = source._endDate = datetime(2026, 10, 20)
                source._cachedFolderName = directory
                source._dataSetId = "dataset"
                result = Path(source.getFileName("macro_calendar"))
                self.assertEqual(result.resolve().parent, (Path(directory) / "dataset").resolve())
                self.assertNotIn("/", result.name)
                self.assertNotIn("\\", result.name)

    def test_plain_currency_cache_filename_is_preserved(self):
        source = adapter.FXMacroDataCalendarDataSource.__new__(adapter.FXMacroDataCalendarDataSource)
        source._currency = "usd"
        source._startDate = source._endDate = datetime(2026, 10, 20)
        source._cachedFolderName = "/cache"
        source._dataSetId = "dataset"
        self.assertEqual(source.getFileName("macro_calendar"), "/cache/dataset/usd_20261020_20261020.json")

    def row_time(self, **row):
        return adapter._event_time(row)

    def test_utc_timestamp(self):
        self.assertEqual(
            self.row_time(announcement_datetime_utc="2026-10-20T12:30:00Z"),
            datetime(2026, 10, 20, 12, 30),
        )

    def test_positive_offset_is_converted(self):
        self.assertEqual(
            self.row_time(announcement_datetime_local="2026-10-20T08:30:00+02:00"),
            datetime(2026, 10, 20, 6, 30),
        )

    def test_negative_offset_is_converted(self):
        self.assertEqual(
            self.row_time(announcement_datetime_local="2026-10-20T08:30:00-04:00"),
            datetime(2026, 10, 20, 12, 30),
        )

    def test_fractional_seconds_are_preserved(self):
        self.assertEqual(
            self.row_time(
                announcement_datetime_utc="2026-10-20T12:30:00.123456Z"
            ).microsecond,
            123456,
        )

    def test_naive_local_time_is_unavailable(self):
        self.assertIsNone(
            self.row_time(announcement_datetime_local="2026-10-20T08:30:00")
        )

    def test_naive_explicit_utc_time(self):
        self.assertEqual(
            self.row_time(announcement_datetime_utc="2026-10-20T12:30:00"),
            datetime(2026, 10, 20, 12, 30),
        )

    def test_reference_date_is_not_an_event_time(self):
        self.assertIsNone(self.row_time(date="2026-10-20"))

    def test_date_only_is_unavailable(self):
        self.assertIsNone(self.row_time(announcement_datetime_utc="2026-10-20"))

    def test_malformed_timestamp_is_unavailable(self):
        for value in ("invalid", 123, "2026-10-20T12:30:00garbage"):
            with self.subTest(value=value):
                self.assertIsNone(self.row_time(announcement_datetime_utc=value))

    def test_utc_field_takes_precedence(self):
        self.assertEqual(
            self.row_time(
                announcement_datetime_utc="2026-10-20T12:30:00Z",
                announcement_datetime_local="2026-10-20T20:30:00+08:00",
            ),
            datetime(2026, 10, 20, 12, 30),
        )

    def download(self, payload, top_only=True):
        source = adapter.FXMacroDataCalendarDataSource.__new__(
            adapter.FXMacroDataCalendarDataSource
        )
        source._currency = "usd"
        source._startDate = source._endDate = datetime(2026, 10, 20)
        source._topTierOnly = top_only
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "calendar.json")
            with (
                patch.object(adapter, "urlopen", return_value=response),
                patch.dict("os.environ", {"FXMD_API_KEY": ""}),
            ):
                source.downloadAndAdjustData("macro_calendar", path)
            return json.loads(Path(path).read_text())

    def test_valid_rows_are_cached(self):
        self.assertEqual(
            self.download({"data": [{"release": "cpi", "market_tier": 1}]}),
            [{"release": "cpi", "market_tier": 1}],
        )

    def test_string_false_and_boolean_tier_are_not_top_tier(self):
        self.assertEqual(
            self.download(
                {"data": [{"top_tier_for_currency": "false"}, {"market_tier": True}]}
            ),
            [],
        )

    def test_explicit_top_tier_is_kept(self):
        self.assertEqual(
            len(self.download({"data": [{"top_tier_for_currency": True}]})), 1
        )

    def test_error_payload_does_not_expose_details(self):
        with self.assertRaises(ValueError) as caught:
            self.download({"detail": "sensitive upstream text"})
        self.assertNotIn("sensitive upstream text", str(caught.exception))

    def test_error_envelope_cannot_be_cached_as_empty_success(self):
        with self.assertRaises(ValueError):
            self.download({"error": "failed", "data": []})

    def test_transport_error_is_sanitized(self):
        source = adapter.FXMacroDataCalendarDataSource.__new__(
            adapter.FXMacroDataCalendarDataSource
        )
        source._currency = "usd"
        source._startDate = source._endDate = datetime(2026, 10, 20)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                adapter, "urlopen", side_effect=URLError("sensitive upstream text")
            ),
        ):
            with self.assertRaises(ValueError) as caught:
                source.downloadAndAdjustData(
                    "macro_calendar", str(Path(directory) / "calendar.json")
                )
        self.assertNotIn("sensitive upstream text", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
