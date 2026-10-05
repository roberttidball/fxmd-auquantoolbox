import json
import os
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backtester.dataSource.data_source import DataSource
from backtester.instrumentUpdates import StockInstrumentUpdate


class FXMacroDataCalendarDataSource(DataSource):
    """Macro release calendar data source backed by FXMacroData."""

    def __init__(
        self,
        cachedFolderName,
        dataSetId,
        instrumentIds,
        startDateStr,
        endDateStr,
        currency="usd",
        topTierOnly=True,
    ):
        self._currency = currency.lower()
        self._topTierOnly = topTierOnly
        super(FXMacroDataCalendarDataSource, self).__init__(
            cachedFolderName,
            dataSetId,
            instrumentIds,
            startDateStr,
            endDateStr,
        )
        self._allTimes, self._groupedInstrumentUpdates = self.getGroupedInstrumentUpdates()
        self.processAllInstrumentUpdates(pad=False)

    def getAllInstrumentIds(self):
        return ["macro_calendar"]

    def downloadAndAdjustData(self, instrumentId, fileName):
        if os.path.isfile(fileName):
            return True
        params = {
            "start_date": self._startDate.strftime("%Y-%m-%d"),
            "end_date": self._endDate.strftime("%Y-%m-%d"),
        }
        headers = {"Accept": "application/json", "User-Agent": "auquantoolbox-fxmacrodata"}
        api_key = (os.getenv("FXMD_API_KEY") or "").strip()
        if any(ch.isspace() or not ch.isprintable() for ch in api_key):
            raise ValueError("FXMD_API_KEY contains whitespace or control characters")
        url = "https://api.fxmacrodata.com/v1/calendar/%s?%s" % (
            self._currency,
            urlencode(params),
        )
        request = Request(url, headers=headers)
        if api_key:
            # Unredirected headers are not copied onto a redirected request.
            request.add_unredirected_header("X-API-Key", api_key)
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise ValueError("Unexpected FXMacroData calendar response%s" % (": %s" % detail if detail else ""))
        if self._topTierOnly:
            rows = [row for row in rows if row.get("top_tier_for_currency") or row.get("market_tier") == 1]
        with open(fileName, "w") as handle:
            json.dump(rows, handle)
        return True

    def getFileName(self, instrumentId):
        return os.path.join(
            self._cachedFolderName,
            self._dataSetId,
            "%s_%s_%s.json" % (self._currency, self._startDate.strftime("%Y%m%d"), self._endDate.strftime("%Y%m%d")),
        )

    def getInstrumentUpdateFromRow(self, instrumentId, row):
        event_time = _event_time(row)
        if event_time is None:
            raise ValueError("FXMacroData row has no announcement timestamp")
        bookData = {
            "release": row.get("release"),
            "name": row.get("name"),
            "market_tier": row.get("market_tier"),
            "top_tier_for_currency": bool(row.get("top_tier_for_currency")),
            "actual": row.get("actual"),
            "previous": row.get("previous"),
            "consensus": row.get("consensus"),
        }
        return StockInstrumentUpdate(
            stockInstrumentId=instrumentId,
            tradeSymbol=instrumentId,
            timeOfUpdate=event_time.replace(tzinfo=None),
            bookData=bookData,
        )

    def getGroupedInstrumentUpdates(self):
        allInstrumentUpdates = []
        for instrumentId in self._instrumentIds:
            fileName = self.getFileName(instrumentId)
            if not self.downloadAndAdjustData(instrumentId, fileName):
                continue
            with open(fileName) as handle:
                for row in json.load(handle):
                    allInstrumentUpdates.append(self.getInstrumentUpdateFromRow(instrumentId, row))
        from backtester.dataSource.data_source_utils import groupAndSortByTimeUpdates

        return groupAndSortByTimeUpdates(allInstrumentUpdates)


def _event_time(row):
    value = row.get("announcement_datetime_utc") or row.get("announcement_datetime_local")
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    for suffix in ("+00:00", "-00:00"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    if "." in text:
        text = text.split(".", 1)[0]
    return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
