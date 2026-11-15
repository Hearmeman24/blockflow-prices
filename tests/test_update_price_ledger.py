"""update_price_ledger.py against synthetic source pages.

tests/fixtures/sources/ holds invented pages with the same structure as the
real ones (Kie's paginated JSON price list, PiAPI's Markdown docs), and
tests/fixtures/model-map.json maps them. Every name and price there is fake.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import price_ledger  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
SOURCES = FIX / "sources"
TEST_MAP = FIX / "model-map.json"

spec = importlib.util.spec_from_file_location("update_price_ledger", ROOT / "update_price_ledger.py")
upd = importlib.util.module_from_spec(spec)
sys.modules["update_price_ledger"] = upd
spec.loader.exec_module(upd)

KIE_ENTRIES, PIAPI_ENTRIES = 7, 6


@pytest.fixture
def work(tmp_path):
    """A copy of the synthetic pages we can tamper with, and a ledger path (not created yet)."""
    pages = tmp_path / "pages"
    shutil.copytree(SOURCES, pages)
    return pages, tmp_path / "prices.json", tmp_path / "report.md"


def run(work, *extra, model_map=TEST_MAP):
    pages, ledger, report = work
    code = upd.run(["--ledger", str(ledger), "--map", str(model_map), "--fixtures", str(pages),
                    "--report", str(report), *extra])
    data = json.loads(ledger.read_text()) if ledger.exists() else None
    return code, data, report.read_text()


def entry(data, provider, model, res=None, variant=None, unit=None):
    for e in data["entries"]:
        if (e["provider"], e["model"], e["resolution"], e["variant"]) == (provider, model, res, variant) and (
                unit is None or e["unit"] == unit):
            return e
    return None


def edit_kie(pages, description, **changes):
    for p in (1, 2):
        path = pages / f"kie-page-{p}.json"
        data = json.loads(path.read_text())
        for r in data["data"]["records"]:
            if r["modelDescription"] == description:
                r.update(changes)
        path.write_text(json.dumps(data))


def edit_doc(pages, name, old, new):
    path = pages / name
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


# ------------------------------------------------------------------ parsing


def test_markdown_table_after_anchor_tolerates_blank_lines_and_spacing():
    text = ("intro\n| a | b |\n|---|---|\n| no | 1 |\n\nPrice per second\n\n  | Model | 480p | 720 p|\n\n"
            "  | --- | --- | --- |\n\n  | x | $0.10 | $0.20 |\n\n  | y | 1 | |\n\nafter text\n| z | 9 | 9 |")
    assert upd.markdown_tables_after(text, "Price per second") == [
        ["Model", "480p", "720 p"], ["x", "$0.10", "$0.20"], ["y", "1", ""]]
    assert upd.markdown_tables_after(text, "missing anchor") is None


def test_numbers_are_parsed_defensively():
    assert upd.number("$0.10") == 0.10
    assert upd.number(" 0.1667 ") == 0.1667
    assert upd.number("N/A") is None
    assert upd.number("") is None
    assert upd.number("0") is None
    assert upd.number("\\$1,200") == 1200


def test_kie_pagination_reads_every_page():
    pages_seen = []

    def fetch(method, url, body):
        page = json.loads(body)["pageNum"]
        pages_seen.append(page)
        return (SOURCES / f"kie-page-{page}.json").read_bytes()

    rows = upd.fetch_kie_rows(fetch)
    assert pages_seen == [1, 2]
    assert len(rows) == 9 == json.loads((SOURCES / "kie-page-1.json").read_text())["data"]["total"]


def test_kie_request_shape_is_public_and_generic():
    calls = []

    def fetch(method, url, body):
        calls.append((method, url, json.loads(body)))
        return (SOURCES / f"kie-page-{len(calls)}.json").read_bytes()

    upd.fetch_kie_rows(fetch)
    method, url, body = calls[0]
    assert (method, url) == ("POST", "https://api.kie.ai/client/v1/model-pricing/page")
    assert body == {"pageNum": 1, "pageSize": 100, "modelDescription": "", "interfaceType": ""}
    assert "compatible" in upd.USER_AGENT and "blockflow" not in upd.USER_AGENT.lower()


def test_kie_incomplete_list_is_an_error():
    page1 = json.loads((SOURCES / "kie-page-1.json").read_text())
    page1["data"]["pages"] = 1  # claims one page but the total says more rows exist
    with pytest.raises(upd.SourceError, match="incomplete"):
        upd.fetch_kie_rows(lambda m, u, b: json.dumps(page1).encode())


@pytest.mark.parametrize("payload", [b"<html>", b'{"code": 401, "msg": "no"}', b'{"code": 200, "data": {}}',
                                     b'{"code": 200, "data": {"records": [], "pages": "x", "total": 0}}'])
def test_kie_bad_pages_are_errors(payload):
    with pytest.raises(upd.SourceError):
        upd.fetch_kie_rows(lambda m, u, b: payload)


def test_kie_mapping_and_unmapped_rows():
    rows = []
    for p in (1, 2):
        rows += json.loads((SOURCES / f"kie-page-{p}.json").read_text())["data"]["records"]
    kie_map = json.loads(TEST_MAP.read_text())["kie"]
    result = upd.map_kie(rows, kie_map)
    assert not result.missing
    assert len(result.entries) == KIE_ENTRIES
    e = next(x for x in result.entries if x["model"] == "example/video-model" and x["resolution"] == "720p"
             and x["variant"] is None)
    assert (e["amount"], e["usd"], e["unit"]) == (40, 0.20, "credits_per_second")
    assert e["source_row"] == "Example Video Model, 720p no video input"
    # one row, two targets (1K and 2K share a price)
    assert {x["resolution"] for x in result.entries if x["model"] == "example/image-model"
            and x["unit"] == "credits_per_image"} == {"1k", "2k", "4k"}
    # a watched new row is reported; an ignored mode and an unwatched chat model are not
    assert result.unmapped == ["Example Video Model Next, 720p"]


def test_piapi_mapping_tables_patterns_and_unmapped_rows():
    doc = json.loads(TEST_MAP.read_text())["piapi"]["docs"][0]
    result = upd.map_piapi_doc((SOURCES / "piapi-example-api_video.md").read_text(), doc)
    got = {(e["model"], e["resolution"], e["variant"]): e["amount"] for e in result.entries}
    assert got == {("example-video", "480p", None): 0.10, ("example-video", "720p", None): 0.20,
                   ("example-video", "1080p", None): 0.50, ("example-video-fast", "480p", None): 0.05,
                   ("example-video-fast", "720p", None): 0.10, ("example:1.0", "720p", "video_input"): 0.08}
    assert result.unmapped == ["example-api/video: 0.9"]  # the ignored legacy row is not listed
    assert result.missing == ["example-api/video: 2.0"]  # mapped, but no longer on the page
    image = json.loads(TEST_MAP.read_text())["piapi"]["docs"][1]
    result = upd.map_piapi_doc((SOURCES / "piapi-example-api_image.md").read_text(), image)
    assert [(e["model"], e["unit"], e["amount"]) for e in result.entries] == [
        ("example-upscale", "usd_per_megapixel", 0.002)]


# ------------------------------------------------------------------ full runs


def test_first_run_writes_a_full_valid_ledger(work):
    code, data, report = run(work)
    assert code == upd.EXIT_OK
    price_ledger.validate_ledger(data)
    assert len(data["entries"]) == KIE_ENTRIES + PIAPI_ENTRIES + 1
    assert entry(data, "kie", "example/video-model", "720p", "video_input")["amount"] == 25
    assert data["sources"]["kie_pricing_api"]["url"] == "https://kie.ai/pricing"
    assert upd.KIE_PAGE_URL in data["sources"]["kie_pricing_api"]["note"]
    assert data["sources"]["piapi_example_video"]["url"] == "https://piapi.ai/docs/example-api/video.md"
    assert data["not_priceable"] == json.loads(TEST_MAP.read_text())["not_priceable"]
    assert "14 added, 0 removed, 0 changed" in report
    assert "## Added" in report and "example/image-model" in report
    assert "## Unmapped provider rows" in report and "Example Video Model Next, 720p" in report
    assert "## Mapped rows the provider no longer lists" in report and "example-api/video: 2.0" in report


def test_unchanged_prices_do_not_rewrite_the_file(work):
    run(work)
    _, ledger, _ = work
    before = ledger.read_text()
    code, _, report = run(work)
    assert code == upd.EXIT_OK
    assert ledger.read_text() == before
    assert "Ledger unchanged" in report


def test_check_mode(work):
    run(work)
    _, ledger, _ = work
    data = json.loads(ledger.read_text())
    data["entries"] = data["entries"][1:]  # the committed file lacks a price
    ledger.write_text(price_ledger.dumps(data))
    stale = ledger.read_text()
    code, _, _ = run(work, "--check")
    assert code == upd.EXIT_OUTDATED
    assert ledger.read_text() == stale  # --check never writes
    run(work)
    assert run(work, "--check")[0] == upd.EXIT_OK


def test_price_change_is_reported_with_percentage(work):
    run(work)
    pages, _, _ = work
    edit_kie(pages, "Example Video Model, 1080p no video input", creditPrice="125")
    code, ledger, report = run(work)
    assert code == upd.EXIT_OK
    assert entry(ledger, "kie", "example/video-model", "1080p")["amount"] == 125
    assert "| kie example/video-model 1080p (credits_per_second) | 100 | 125 | +25.0% |" in report


def test_large_jump_is_written_but_flagged_for_review(work):
    run(work)
    pages, _, _ = work
    edit_doc(pages, "piapi-example-api_video.md", "| example-video | $0.10 | $0.20 | $0.50 |",
             "| example-video | $0.10 | $0.45 | $0.50 |")
    code, ledger, report = run(work)
    assert code == upd.EXIT_NEEDS_REVIEW
    assert entry(ledger, "piapi", "example-video", "720p")["amount"] == 0.45
    assert "## Needs review" in report and "0.2 → 0.45" in report


@pytest.mark.parametrize("damage", ["missing", "garbage", "few"])
def test_a_failed_source_keeps_its_old_values_and_exits_2(work, damage):
    run(work)
    pages, ledger, _ = work
    before = json.loads(ledger.read_text())
    kie_before = [e for e in before["entries"] if e["source"] == "kie_pricing_api"]
    if damage == "missing":
        (pages / "kie-page-2.json").unlink()
    elif damage == "garbage":
        (pages / "kie-page-1.json").write_text("<html>maintenance</html>")
    else:  # the provider renamed most rows: far fewer prices than expected
        for p in (1, 2):
            data = json.loads((pages / f"kie-page-{p}.json").read_text())
            for r in data["data"]["records"][1:]:
                r["modelDescription"] = "renamed " + r["modelDescription"]
            (pages / f"kie-page-{p}.json").write_text(json.dumps(data))
    code, after, report = run(work)
    assert code == upd.EXIT_SOURCE_FAILED
    assert [e for e in after["entries"] if e["source"] == "kie_pricing_api"] == kie_before
    assert "## Sources that failed (previous values kept)" in report and "kie_pricing_api" in report


def test_a_failed_piapi_doc_does_not_affect_kie(work):
    run(work)
    pages, _, _ = work
    (pages / "piapi-example-api_video.md").write_text("# moved\nnothing here")
    code, after, report = run(work)
    assert code == upd.EXIT_SOURCE_FAILED
    assert entry(after, "piapi", "example-video", "720p")["amount"] == 0.20
    assert entry(after, "kie", "example/video-model", "720p")["amount"] == 40
    assert "piapi_example_video" in report


def test_failure_wins_over_review(work):
    run(work)
    pages, _, _ = work
    (pages / "kie-page-1.json").write_text("nope")
    edit_doc(pages, "piapi-example-api_video.md", "| example-video | $0.10 | $0.20 | $0.50 |",
             "| example-video | $0.10 | $0.90 | $0.50 |")
    code, _, report = run(work)
    assert code == upd.EXIT_SOURCE_FAILED
    assert "Needs review" in report


def test_first_run_with_a_broken_source_never_writes_a_half_empty_ledger(work):
    pages, ledger, _ = work
    (pages / "kie-page-1.json").write_text("nope")
    code, data, report = run(work)
    assert code == upd.EXIT_SOURCE_FAILED
    assert data is None and not ledger.exists()
    assert "Not written" in report


def test_a_note_for_a_model_that_now_has_a_price_is_flagged(work, tmp_path):
    model_map = json.loads(TEST_MAP.read_text())
    model_map["not_priceable"].append({"provider": "kie", "model": "example/image-model", "code": "no_published_price",
                                       "message": "Old note.", "source": "https://example.test/image"})
    path = tmp_path / "map.json"
    path.write_text(json.dumps(model_map))
    _, _, report = run(work, model_map=path)
    assert "## Not-priceable notes that now have a price" in report and "example/image-model" in report


# ------------------------------------------------------------------ the real files


def test_the_real_ledger_and_map_are_valid_and_consistent():
    ledger = price_ledger.load_bundled()
    model_map = price_ledger.validate_model_map(json.loads(price_ledger.MODEL_MAP_PATH.read_text()))
    mapped = {(("kie", t["model"])) for r in model_map["kie"]["rows"] for t in r["targets"]}
    for doc in model_map["piapi"]["docs"]:
        mapped |= {("piapi", m) for t in doc.get("tables", []) for m in t["rows"].values()}
        mapped |= {("piapi", t["model"]) for p in doc.get("patterns", []) for t in p["targets"]}
    for e in ledger.entries:
        assert (e.provider, e.model) in mapped, f"{e.provider} {e.model} has no mapping"


def test_workflow_is_minimal_pinned_and_opt_in():
    wf = (ROOT / ".github" / "workflows" / "update-prices.yml").read_text()
    assert "workflow_dispatch:" in wf and "schedule:" in wf
    assert "contents: write" in wf and "pull-requests: write" in wf and "issues: write" in wf
    for line in wf.splitlines():
        if "uses:" in line:
            ref = line.split("uses:", 1)[1].split("#", 1)[0].strip()
            assert "@" in ref and len(ref.split("@", 1)[1]) == 40, f"not pinned to a commit SHA: {line}"
    assert "python3 update_price_ledger.py --report" in wf
    assert "vars.PRICE_LEDGER_AUTO_MERGE == 'true'" in wf and "steps.run.outputs.code == '0'" in wf
    assert "BRANCH: automation/price-ledger" in wf
