"""The price ledger format and its strict validator (price_ledger.py)."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import price_ledger  # noqa: E402
from price_ledger import LedgerError, validate_ledger  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def good() -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-11-15T10:00:00Z",
        "sources": {
            "kie_pricing_api": {"url": "https://api.kie.ai/client/v1/model-pricing/page",
                                "fetched_at": "2026-11-15T10:00:00Z"},
            "piapi_seedance_2": {"url": "https://piapi.ai/docs/seedance-api/seedance-2.md",
                                 "fetched_at": "2026-11-14T09:00:00Z"},
        },
        "entries": [
            {"provider": "kie", "model": "bytedance/seedance-2", "resolution": "720p", "variant": None,
             "unit": "credits_per_second", "amount": 41, "usd": 0.205, "source": "kie_pricing_api",
             "source_row": "bytedance/seedance-2, 720p no video input"},
            {"provider": "piapi", "model": "seedance-2", "resolution": "720p", "variant": None,
             "unit": "usd_per_second", "amount": 0.2, "source": "piapi_seedance_2",
             "source_row": "seedance-2 | 720p"},
        ],
    }


def test_a_good_ledger_validates_and_indexes():
    ledger = validate_ledger(good())
    assert ledger.schema_version == 1
    assert ledger.generated_at == "2026-11-15T10:00:00Z"
    assert len(ledger.entries) == 2
    e = ledger.find("kie", "bytedance/seedance-2", "credits_per_second", "720p", None)
    assert e.amount == 41 and e.usd == 0.205 and e.as_of == "2026-11-15"
    assert ledger.find("piapi", "seedance-2", "usd_per_second", "720p", None).as_of == "2026-11-14"
    assert ledger.find("piapi", "seedance-2", "usd_per_second", "1080p", None) is None


def test_validation_does_not_mutate_its_input():
    data = good()
    before = copy.deepcopy(data)
    validate_ledger(data)
    assert data == before


def _bad(mutate):
    data = good()
    mutate(data)
    return data


BAD = {
    "not an object": lambda d: d.clear() or d.update({"x": 1}),
    "future schema": lambda d: d.update(schema_version=2),
    "schema as string": lambda d: d.update(schema_version="1"),
    "bad generated_at": lambda d: d.update(generated_at="yesterday"),
    "sources not object": lambda d: d.update(sources=[]),
    "source url http": lambda d: d["sources"]["kie_pricing_api"].update(url="http://kie.ai/pricing"),
    "source without fetched_at": lambda d: d["sources"]["kie_pricing_api"].pop("fetched_at"),
    "entries not list": lambda d: d.update(entries={}),
    "no entries": lambda d: d.update(entries=[]),
    "unknown provider": lambda d: d["entries"][0].update(provider="fal"),
    "unknown unit": lambda d: d["entries"][0].update(unit="credits_per_token"),
    "unit wrong for provider": lambda d: d["entries"][0].update(unit="usd_per_second"),
    "zero amount": lambda d: d["entries"][0].update(amount=0),
    "negative amount": lambda d: d["entries"][0].update(amount=-1),
    "string amount": lambda d: d["entries"][0].update(amount="41"),
    "bool amount": lambda d: d["entries"][0].update(amount=True),
    "huge amount": lambda d: d["entries"][0].update(amount=1e9),
    "bad usd": lambda d: d["entries"][0].update(usd=-0.2),
    "empty model": lambda d: d["entries"][0].update(model=" "),
    "bad resolution": lambda d: d["entries"][0].update(resolution="720 P"),
    "bad variant": lambda d: d["entries"][0].update(variant="With Video"),
    "unknown source": lambda d: d["entries"][0].update(source="nowhere"),
    "extra field": lambda d: d["entries"][0].update(prompt="hi"),
    "duplicate entry": lambda d: d["entries"].append(dict(d["entries"][0])),
    "missing source_row": lambda d: d["entries"][0].pop("source_row"),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_every_kind_of_bad_ledger_is_rejected(name):
    with pytest.raises(LedgerError):
        validate_ledger(_bad(BAD[name]))


def test_non_dict_and_non_json_are_rejected(tmp_path):
    with pytest.raises(LedgerError):
        validate_ledger([1, 2])
    path = tmp_path / "p.json"
    path.write_text("{nope", encoding="utf-8")
    with pytest.raises(LedgerError):
        price_ledger.load_file(path)


def test_bundled_ledger_and_model_map_are_valid():
    ledger = price_ledger.load_bundled()
    assert ledger.entries
    providers = {e.provider for e in ledger.entries}
    assert providers == {"kie", "piapi"}
    model_map = json.loads(price_ledger.MODEL_MAP_PATH.read_text(encoding="utf-8"))
    price_ledger.validate_model_map(model_map)


def test_every_bundled_entry_has_an_https_source_and_a_date():
    ledger = price_ledger.load_bundled()
    for entry in ledger.entries:
        assert ledger.sources[entry.source]["url"].startswith("https://")
        assert len(entry.as_of) == 10 and entry.as_of[4] == "-"


def test_the_test_model_map_is_valid():
    price_ledger.validate_model_map(json.loads((FIXTURES / "model-map.json").read_text(encoding="utf-8")))


# ------------------------------------------------------------------ not-priceable notes


def test_bundled_notes_are_valid_and_match_the_model_map():
    data = json.loads(price_ledger.BUNDLED_PATH.read_text())
    model_map = json.loads(price_ledger.MODEL_MAP_PATH.read_text())
    assert data["not_priceable"] == model_map["not_priceable"]
    ledger = price_ledger.validate_ledger(data)
    note = ledger.note("kie", "kling-4.0/flash-reference-to-video", "video_input")
    assert note.code == "input_video_billing_unknown"
    assert ledger.note("kie", "gpt-image-2-6/flare-text-to-image").code == "priced_per_token"
    assert ledger.note("kie", "bytedance/seedance-2") is None


@pytest.mark.parametrize("bad", [{"code": "because"}, {"provider": "fal"}, {"message": ""},
                                 {"source": "http://x"}, {"variant": "With Video"}, {"extra": 1}])
def test_bad_notes_are_rejected(bad):
    data = json.loads(price_ledger.BUNDLED_PATH.read_text())
    data["not_priceable"][0].update(bad)
    with pytest.raises(LedgerError):
        validate_ledger(data)
