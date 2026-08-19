"""SEC EDGAR filing-flag provider -- the missing half of gate A4.

Every price-and-fundamentals vendor probed for this system fails the same way:
none carries restatement, late-filing or auditor-change status. Gate A4 has
therefore returned DATA_GAP on every candidate, which means **no name has ever
been able to reach a full PASS**. That is not a cosmetic gap; it is the reason
the screen returns nothing on live data.

EDGAR carries all three as *structured* fields, not prose:

  * **Restatement** -- an 8-K carrying item **4.02**, "Non-Reliance on
    Previously Issued Financial Statements". This is the canonical restatement
    announcement and is exactly what A4 is asking about.
  * **Auditor change** -- an 8-K carrying item **4.01**, "Changes in
    Registrant's Certifying Accountant". Stronger than diffing auditor names
    across vendor snapshots, because it is the event itself, and item 4.01(b)
    filings carry the engagement of the *new* accountant.
  * **Late filing** -- a form **NT 10-K** or **NT 10-Q**, the notification of
    late filing.

All three come from one JSON document per company, so a whole universe costs
one request per name and no parsing of filing text.

Going-concern language is deliberately NOT sourced here. Full-text searching a
10-K for "going concern" over-flags badly: the phrase appears in accounting-
policy boilerplate and in negative constructions ("no substantial doubt"). The
audit opinion code carried by the fundamentals provider is the better signal
and is merged in separately.

VERIFICATION STATUS: written against EDGAR's documented submissions API but
**not exercised against the live service**, because this build environment's
egress policy refuses sec.gov. Run ``EdgarFilingFlags.self_test(cik)`` from a
permitted network before relying on it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

import requests

from ..types import FilingFlags
from .base import DataUnavailable

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"

# 8-K item numbers. These are the events themselves, not descriptions of them.
ITEM_RESTATEMENT = "4.02"
ITEM_AUDITOR_CHANGE = "4.01"

# Notification of late filing.
LATE_FORMS = {"NT 10-K", "NT 10-Q", "NT 20-F", "NT 10-K/A", "NT 10-Q/A"}

# SEC asks for a descriptive User-Agent with contact details and throttles at
# roughly ten requests a second. Both are conditions of use, not suggestions.
MAX_REQUESTS_PER_SECOND = 8.0


@dataclass
class EdgarFilingFlags:
    """Fetches Module A4 red flags for one company at a time.

    ``user_agent`` must identify the operator, per SEC's access policy -- they
    block requests without one. It is required rather than defaulted so a
    deployment cannot silently violate the terms.
    """

    user_agent: str
    session: Any = None
    timeout: float = 20.0
    restatement_lookback_years: int = 3
    auditor_change_lookback_months: int = 12
    late_filing_lookback_months: int = 12
    # A filer's 8-K history does not change hourly. Caching keeps a weekly
    # universe screen to one request per name instead of one per lookup, which
    # is the difference between a 50-second overlay and an unusable one.
    cache_days: int = 7
    cache: dict = field(default_factory=dict)
    _last_request: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        if not self.user_agent or "@" not in self.user_agent:
            raise ValueError(
                "EDGAR requires a User-Agent identifying the operator, including a contact "
                'address -- for example "GCFP Research research@example.com". '
                "Requests without one are refused by sec.gov."
            )
        if self.session is None:
            self.session = requests.Session()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        minimum = 1.0 / MAX_REQUESTS_PER_SECOND
        if elapsed < minimum:
            time.sleep(minimum - elapsed)
        self._last_request = time.monotonic()

    def fetch_submissions(self, cik: str) -> dict:
        self._throttle()
        url = SUBMISSIONS_URL.format(cik=str(cik).lstrip("CIK").strip())
        response = self.session.get(
            url,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=self.timeout,
        )
        if response.status_code == 404:
            raise DataUnavailable(str(cik), "EDGAR submissions", "no filer with that CIK")
        response.raise_for_status()
        return response.json()

    def get_flags(self, cik: Optional[str], as_of: Optional[date] = None) -> FilingFlags:
        if not cik:
            return FilingFlags(evidence=("EDGAR: no CIK on the profile, overlay not run",))

        key = str(cik).lstrip("CIK").strip()
        today = as_of or date.today()
        cached = self.cache.get(key)
        if cached is not None:
            fetched_on, payload = cached
            if (today - fetched_on).days < self.cache_days:
                return self.parse_submissions(payload, as_of=as_of)

        try:
            payload = self.fetch_submissions(cik)
        except (DataUnavailable, requests.RequestException) as exc:
            # An outage must not read as a clean bill of health.
            return FilingFlags(evidence=(f"EDGAR: unavailable ({exc})",))

        self.cache[key] = (today, payload)
        return self.parse_submissions(payload, as_of=as_of)

    def parse_submissions(self, payload: dict, as_of: Optional[date] = None) -> FilingFlags:
        """Read the three structured A4 signals out of a submissions document.

        Kept separate from the fetch so it is testable against a recorded
        payload without touching the network -- which is the only way it can be
        tested at all from this environment.
        """
        as_of = as_of or date.today()
        recent = ((payload or {}).get("filings") or {}).get("recent") or {}

        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        items = recent.get("items") or []
        accessions = recent.get("accessionNumber") or []

        if not forms:
            return FilingFlags(evidence=("EDGAR: submissions document carried no filings",))

        restatement_cutoff = as_of - timedelta(days=365 * self.restatement_lookback_years)
        auditor_cutoff = as_of - timedelta(days=int(30.4375 * self.auditor_change_lookback_months))
        late_cutoff = as_of - timedelta(days=int(30.4375 * self.late_filing_lookback_months))

        restatement = False
        auditor_change = False
        late = False
        evidence: list[str] = []

        for index, form in enumerate(forms):
            filed = _parse_date(dates[index] if index < len(dates) else None)
            if filed is None or filed > as_of:
                continue
            entry_items = str(items[index]) if index < len(items) and items[index] else ""
            accession = accessions[index] if index < len(accessions) else "?"
            item_set = {piece.strip() for piece in entry_items.split(",") if piece.strip()}

            if form.startswith("8-K"):
                if ITEM_RESTATEMENT in item_set and filed >= restatement_cutoff:
                    restatement = True
                    evidence.append(f"EDGAR: 8-K item 4.02 (non-reliance) filed {filed.isoformat()} [{accession}]")
                if ITEM_AUDITOR_CHANGE in item_set and filed >= auditor_cutoff:
                    auditor_change = True
                    evidence.append(f"EDGAR: 8-K item 4.01 (auditor change) filed {filed.isoformat()} [{accession}]")

            if form in LATE_FORMS and filed >= late_cutoff:
                late = True
                evidence.append(f"EDGAR: {form} (late filing) filed {filed.isoformat()} [{accession}]")

        if not evidence:
            evidence.append(
                f"EDGAR: {len(forms)} filings scanned to {as_of.isoformat()}; "
                "no 4.02, 4.01 or NT filings in the lookback windows"
            )

        return FilingFlags(
            restatement_within_lookback=restatement,
            auditor_change_within_lookback=auditor_change,
            auditor_change_reason=None,
            # Not sourced here on purpose -- see the module docstring.
            going_concern_language=None,
            delayed_filing=late,
            as_of=as_of,
            evidence=tuple(evidence),
        )

    def self_test(self, cik: str = "0000320193") -> FilingFlags:
        """Fetch one real filer, to confirm the API shape on a permitted network.

        This module has never run against live EDGAR. Call this first.
        """
        payload = self.fetch_submissions(cik)
        name = payload.get("name", "?")
        flags = self.parse_submissions(payload)
        print(f"EDGAR reachable. {name} (CIK {cik}):")
        for line in flags.evidence:
            print(f"  {line}")
        return flags


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
