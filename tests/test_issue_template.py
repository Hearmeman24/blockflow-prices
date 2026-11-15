"""The "Report a price change" form must accept exactly what the app's link prefills."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / ".github" / "ISSUE_TEMPLATE" / "price-change.yml"

# The query parameters BlockFlow's "report a price change" link sends
# (backend/byok/learned_prices.py, REPORT_FIELDS in the app). Keep in sync.
APP_FIELDS = ("provider", "model", "variant", "unit", "ledger_price", "ledger_date", "observed_price", "observed_date")


def test_form_fields_match_the_app_link():
    text = TEMPLATE.read_text(encoding="utf-8")
    ids = re.findall(r"^\s+id:\s*(\S+)\s*$", text, flags=re.M)
    assert ids[: len(APP_FIELDS)] == list(APP_FIELDS)
    assert set(ids) - set(APP_FIELDS) == {"notes"}  # the only field the reporter fills in alone


def test_prefilled_fields_are_plain_inputs():
    text = TEMPLATE.read_text(encoding="utf-8")
    blocks = re.split(r"^\s+- type:\s*", text, flags=re.M)[1:]
    kinds = {re.search(r"id:\s*(\S+)", b).group(1): b.split()[0] for b in blocks if "id:" in b}
    assert all(kinds[f] == "input" for f in APP_FIELDS)
