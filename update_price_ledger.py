#!/usr/bin/env python3
"""Regenerate prices.json from the providers' public price lists.

Sources (public, no API key, a handful of requests):

- Kie: the price list behind https://kie.ai/pricing. The page embeds only the
  first 25 of ~670 rows (``__NEXT_DATA__.props.pageProps.pricingSeed``) and
  loads the rest from ``POST https://api.kie.ai/client/v1/model-pricing/page``
  (``{pageNum, pageSize, modelDescription: "", interfaceType: ""}``), which
  the updater pages through 100 rows at a time. Unofficial: it can change.
- PiAPI: the Markdown docs at https://piapi.ai/docs/<path>.md.

``model-map.json`` maps provider rows to the provider model identifiers
BlockFlow uses. The ledger file is rewritten only when a price, a mapping or a
source URL changed, so an unchanged day produces no diff.

Usage::

    update_price_ledger.py [--check] [--report PATH] [--fixtures DIR]

Exit codes: 0 done (or up to date) · 1 ``--check``: the committed file is out
of date · 2 a source could not be fetched/parsed or returned too few prices
(its previous values were kept) · 3 a price moved more than ×2 or ÷2 (written,
needs review). 2 wins over 3; both are explained in the report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import price_ledger  # noqa: E402

EXIT_OK, EXIT_OUTDATED, EXIT_SOURCE_FAILED, EXIT_NEEDS_REVIEW = 0, 1, 2, 3
USER_AGENT = "Mozilla/5.0 (compatible; price-ledger-updater/1.0)"
TIMEOUT_S = 30.0
KIE_PAGE_URL = "https://api.kie.ai/client/v1/model-pricing/page"
KIE_PUBLIC_URL = "https://kie.ai/pricing"
KIE_NOTE = f"Rows read from POST {KIE_PAGE_URL}, the public list behind {KIE_PUBLIC_URL} (unofficial)."
KIE_PAGE_SIZE = 100
KIE_MAX_PAGES = 30
PIAPI_DOC_URL = "https://piapi.ai/docs/{path}.md"
MIN_FRACTION = 0.5  # fewer prices than this share of the expected ones = a failed source
REVIEW_RATIO = 2.0  # a price moving beyond ×2 or ÷2 needs review
MAX_BODY = 5_000_000

Fetch = Callable[[str, str, bytes | None], bytes]


class SourceError(Exception):
    """A source could not be fetched or parsed."""


# ------------------------------------------------------------------ fetching


def http_fetch(method: str, url: str, body: bytes | None) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/markdown, text/plain"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 - fixed https URLs
            data = resp.read(MAX_BODY + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"{method} {url} failed: {type(exc).__name__}") from None
    if len(data) > MAX_BODY:
        raise SourceError(f"{url} returned more than {MAX_BODY} bytes")
    return data


def fixture_fetch(directory: Path) -> Fetch:
    """Serve saved pages: ``kie-page-<n>.json`` and ``piapi-<path with / as _>.md``."""
    def fetch(method: str, url: str, body: bytes | None) -> bytes:
        if url == KIE_PAGE_URL:
            page = json.loads(body or b"{}").get("pageNum", 1)
            path = directory / f"kie-page-{page}.json"
        else:
            doc = url.removeprefix("https://piapi.ai/docs/").removesuffix(".md").replace("/", "_")
            path = directory / f"piapi-{doc}.md"
        try:
            return path.read_bytes()
        except OSError:
            raise SourceError(f"fixture {path.name} is missing") from None
    return fetch


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ parsing


def norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def number(cell: Any) -> float | None:
    text = str(cell).strip().replace("$", "").replace(",", "").replace("\\", "")
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


def fetch_kie_rows(fetch: Fetch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page, pages, total = 1, 1, None
    while page <= pages:
        body = json.dumps({"pageNum": page, "pageSize": KIE_PAGE_SIZE, "modelDescription": "",
                           "interfaceType": ""}).encode()
        try:
            payload = json.loads(fetch("POST", KIE_PAGE_URL, body))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise SourceError(f"Kie price page {page} is not JSON") from None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or payload.get("code") != 200 or not isinstance(data, dict):
            raise SourceError(f"Kie price page {page} has an unexpected shape")
        records = data.get("records")
        if not isinstance(records, list):
            raise SourceError(f"Kie price page {page} has no records list")
        rows.extend(r for r in records if isinstance(r, dict))
        pages_raw, total_raw = data.get("pages"), data.get("total")
        if not isinstance(pages_raw, int) or not isinstance(total_raw, int) or pages_raw < 1:
            raise SourceError(f"Kie price page {page} has no page count")
        pages, total = min(pages_raw, KIE_MAX_PAGES), total_raw
        page += 1
    if total is not None and len(rows) < total and pages < KIE_MAX_PAGES:
        raise SourceError(f"Kie price list is incomplete: {len(rows)} of {total} rows")
    return rows


def markdown_tables_after(text: str, anchor: str) -> list[list[str]] | None:
    """The first Markdown table after ``anchor`` as rows of cells (blank lines
    between rows allowed, separator rows dropped), or None."""
    start = text.find(anchor)
    if start < 0:
        return None
    rows: list[list[str]] = []
    for line in text[start + len(anchor):].splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                rows.append(cells)
        elif rows and stripped:
            break
    return rows or None


@dataclass
class SourceResult:
    key: str
    url: str
    note: str | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)
    expected: int = 0
    unmapped: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # mapped rows the provider no longer lists
    error: str | None = None


def _entry(provider: str, target: Mapping[str, Any], unit: str, amount: float, usd: float | None, source: str,
           row: str) -> dict[str, Any]:
    e = {"provider": provider, "model": target["model"], "resolution": target.get("resolution"),
         "variant": target.get("variant"), "unit": target.get("unit", unit), "amount": amount}
    if usd is not None:
        e["usd"] = usd
    e.update(source=source, source_row=row)
    return e


def map_kie(rows: Sequence[Mapping[str, Any]], kie_map: Mapping[str, Any]) -> SourceResult:
    result = SourceResult(kie_map["source"], KIE_PUBLIC_URL, KIE_NOTE)
    by_desc = {norm(r.get("modelDescription", "")): r for r in rows}
    mapped = set()
    for rule in kie_map["rows"]:
        result.expected += len(rule["targets"])
        desc = norm(rule["description"])
        row = by_desc.get(desc)
        if row is None:
            result.missing.append(rule["description"])
            continue
        mapped.add(desc)
        amount = number(row.get("creditPrice"))
        if amount is None:
            result.missing.append(f"{rule['description']} (no readable price)")
            continue
        usd = number(row.get("usdPrice"))
        for target in rule["targets"]:
            result.entries.append(_entry("kie", target, rule["unit"], amount, usd, result.key,
                                         str(row.get("modelDescription"))))
    watch = [w.lower() for w in kie_map.get("watch", [])]
    ignore = [w.lower() for w in kie_map.get("ignore", [])]
    for desc, row in by_desc.items():
        if desc not in mapped and any(w in desc for w in watch) and not any(i in desc for i in ignore):
            result.unmapped.append(str(row.get("modelDescription")))
    return result


def map_piapi_doc(text: str, doc: Mapping[str, Any]) -> SourceResult:
    url = PIAPI_DOC_URL.format(path=doc["path"])
    result = SourceResult(doc["source"], url)
    ignore = {norm(k) for k in doc.get("ignore_rows", {})}
    for table in doc.get("tables", []):
        rows_map = {norm(k).replace(" ", ""): v for k, v in table["rows"].items()}
        cols_map = {norm(k).replace(" ", ""): v for k, v in table["columns"].items()}
        result.expected += len(rows_map) * len(cols_map)
        cells = markdown_tables_after(text, table["after"])
        if not cells:
            result.missing.append(f"table after {table['after']!r}")
            continue
        header = [norm(c).replace(" ", "") for c in cells[0]]
        found_rows = set()
        for row in cells[1:]:
            label = norm(row[0]).replace(" ", "").strip("`")
            if label in ignore:
                continue
            model = rows_map.get(label)
            if model is None:
                result.unmapped.append(f"{doc['path']}: {row[0]}")
                continue
            found_rows.add(label)
            for col, cell in zip(header[1:], row[1:]):
                res = cols_map.get(col)
                amount = number(cell)
                if res is None or amount is None:
                    continue
                target = {"model": model, "resolution": res, "variant": table.get("variant")}
                result.entries.append(_entry("piapi", target, table["unit"], amount, amount, result.key,
                                             f"{row[0]} | {cells[0][header.index(col)]}"))
        result.missing.extend(f"{doc['path']}: {k}" for k in rows_map if k not in found_rows)
    for pat in doc.get("patterns", []):
        result.expected += len(pat["targets"])
        match = re.search(pat["regex"], text)
        amount = number(match.group(1)) if match else None
        if amount is None:
            result.missing.append(f"{doc['path']}: pattern {pat['regex']!r}")
            continue
        for target in pat["targets"]:
            result.entries.append(_entry("piapi", target, pat["unit"], amount, amount, result.key,
                                         match.group(0)[:200]))
    return result


def collect(fetch: Fetch, model_map: Mapping[str, Any]) -> list[SourceResult]:
    results: list[SourceResult] = []
    try:
        results.append(map_kie(fetch_kie_rows(fetch), model_map["kie"]))
    except SourceError as exc:
        results.append(SourceResult(model_map["kie"]["source"], KIE_PUBLIC_URL, KIE_NOTE, error=str(exc)))
    for doc in model_map["piapi"]["docs"]:
        url = PIAPI_DOC_URL.format(path=doc["path"])
        try:
            text = fetch("GET", url, None).decode("utf-8")
        except (SourceError, UnicodeDecodeError) as exc:
            results.append(SourceResult(doc["source"], url, error=str(exc)))
            continue
        results.append(map_piapi_doc(text, doc))
    return results


# ------------------------------------------------------------------ building


@dataclass
class Outcome:
    ledger: dict[str, Any]
    changed: bool
    failures: list[str]
    review: list[str]
    report: str
    blocked: bool = False  # a failed source had no previous values: nothing may be written


def _key(e: Mapping[str, Any]) -> tuple:
    return (e["provider"], e["model"], e["unit"], e.get("resolution"), e.get("variant"))


def _content(data: Mapping[str, Any]) -> Any:
    """What counts as a change: entries and source URLs, not timestamps."""
    return (sorted(json.dumps(e, sort_keys=True) for e in data.get("entries", [])),
            json.dumps(data.get("not_priceable", []), sort_keys=True),
            sorted((k, v.get("url"), v.get("note")) for k, v in data.get("sources", {}).items()))


def build(old: Mapping[str, Any], results: Sequence[SourceResult], fetched_at: str,
          notes: Sequence[Mapping[str, Any]] = ()) -> Outcome:
    old_entries = list(old.get("entries", []))
    old_sources = dict(old.get("sources", {}))
    entries: list[dict[str, Any]] = []
    sources: dict[str, Any] = {}
    failures: list[str] = []
    blocked = False
    current_keys = {r.key for r in results}
    for r in results:
        previous = [e for e in old_entries if e["source"] == r.key]
        need = max(MIN_FRACTION * r.expected, MIN_FRACTION * len(previous))
        if r.error is None and len(r.entries) < need:
            r.error = f"only {len(r.entries)} prices found, expected at least {int(need + 0.999)}"
        if r.error is not None:
            failures.append(f"{r.key}: {r.error}")
            if previous:
                entries.extend(previous)
                sources[r.key] = old_sources[r.key]
            else:
                blocked = True
            continue
        entries.extend(r.entries)
        sources[r.key] = {"url": r.url, "fetched_at": fetched_at, **({"note": r.note} if r.note else {})}
    produced = {_key(e) for e in entries}
    carried = [e for e in old_entries if e["source"] not in current_keys and _key(e) not in produced]
    for e in carried:
        entries.append(e)
        sources[e["source"]] = old_sources[e["source"]]
    # de-duplicate (a provider may list the same row twice): first wins
    unique: dict[tuple, dict[str, Any]] = {}
    for e in entries:
        unique.setdefault(_key(e), e)
    entries = sorted(unique.values(), key=lambda e: tuple("" if v is None else str(v) for v in _key(e)))
    new = {"schema_version": price_ledger.SCHEMA_VERSION, "generated_at": fetched_at, "sources": sources,
           "entries": entries}
    if notes:
        new["not_priceable"] = [dict(n) for n in notes]
    changed = _content(new) != _content(old)
    if not changed:
        new = dict(old)
    price_ledger.validate_ledger(new)  # never publish an invalid file
    review = _review(old_entries, entries)
    # input_video_billing_unknown notes sit beside a listed rate by design (the rate is known, the rule is not)
    stale = [n for n in notes if n["code"] != "input_video_billing_unknown"
             and any((e["provider"], e["model"]) == (n["provider"], n["model"])
                                     and e.get("variant") == n.get("variant") for e in entries)]
    report = _report(old_entries, entries, results, failures, review, carried, changed, stale)
    if blocked:
        report = report.replace("# Price ledger update\n", "# Price ledger update\n\nNot written: a source "
                                "failed and there are no previous values to keep for it.\n", 1)
    return Outcome(new, changed, failures, review, report, blocked)


def _label(k: tuple) -> str:
    provider, model, unit, res, variant = k
    return " ".join(str(x) for x in (provider, model, res, variant, f"({unit})") if x)


def _review(old: Sequence[Mapping[str, Any]], new: Sequence[Mapping[str, Any]]) -> list[str]:
    before = {_key(e): e["amount"] for e in old}
    flagged = []
    for e in new:
        prev = before.get(_key(e))
        if prev and (e["amount"] / prev > REVIEW_RATIO or e["amount"] / prev < 1 / REVIEW_RATIO):
            flagged.append(f"{_label(_key(e))}: {prev:g} → {e['amount']:g}")
    return flagged


def _report(old, new, results, failures, review, carried, changed, stale=()) -> str:
    before = {_key(e): e for e in old}
    after = {_key(e): e for e in new}
    added = [k for k in after if k not in before]
    removed = [k for k in before if k not in after]
    moved = [k for k in after if k in before and after[k]["amount"] != before[k]["amount"]]
    lines = ["# Price ledger update", ""]
    if failures:
        lines += ["## Sources that failed (previous values kept)", ""] + [f"- {f}" for f in failures] + [""]
    if review:
        lines += ["## Needs review (moved more than ×2 or ÷2)", ""] + [f"- {r}" for r in review] + [""]
    lines += [f"Ledger {'changed' if changed else 'unchanged'}: {len(added)} added, {len(removed)} removed, "
              f"{len(moved)} changed, {len(after)} prices in total.", ""]
    if moved:
        lines += ["## Changed", "", "| Price | Old | New | Change |", "| --- | --- | --- | --- |"]
        for k in sorted(moved, key=_label):
            o, n = before[k]["amount"], after[k]["amount"]
            lines.append(f"| {_label(k)} | {o:g} | {n:g} | {(n - o) / o * 100:+.1f}% |")
        lines.append("")
    if added:
        lines += ["## Added", ""] + [f"- {_label(k)}: {after[k]['amount']:g}" for k in sorted(added, key=_label)]
        lines.append("")
    if removed:
        lines += ["## Removed", ""] + [f"- {_label(k)}: {before[k]['amount']:g}"
                                       for k in sorted(removed, key=_label)] + [""]
    if carried:
        lines += ["## Carried over from older sources (no current source lists them)", ""]
        lines += [f"- {_label(_key(e))} from {e['source']}" for e in carried] + [""]
    if stale:
        lines += ["## Not-priceable notes that now have a price (remove them from model-map.json)", ""]
        lines += [f"- {n['provider']} {n['model']} {n.get('variant') or ''} ({n['code']})".replace("  ", " ")
                  for n in stale] + [""]
    missing = [(r.key, m) for r in results for m in r.missing]
    if missing:
        lines += ["## Mapped rows the provider no longer lists", ""] + [f"- {k}: {m}" for k, m in missing] + [""]
    unmapped = [(r.key, u) for r in results for u in r.unmapped]
    if unmapped:
        lines += ["## Unmapped provider rows (add them to model-map.json if BlockFlow uses them)", ""]
        lines += [f"- {k}: {u}" for k, u in unmapped] + [""]
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------ CLI


def run(argv: Sequence[str] | None = None, fetch: Fetch | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", type=Path, default=price_ledger.BUNDLED_PATH)
    parser.add_argument("--map", type=Path, default=price_ledger.MODEL_MAP_PATH)
    parser.add_argument("--check", action="store_true", help="exit 1 when the ledger file is out of date")
    parser.add_argument("--report", type=Path, help="write a Markdown summary here")
    parser.add_argument("--fixtures", type=Path, help="read saved pages from this directory instead of the web")
    args = parser.parse_args(argv)

    model_map = price_ledger.validate_model_map(json.loads(args.map.read_text(encoding="utf-8")))
    old = json.loads(args.ledger.read_text(encoding="utf-8")) if args.ledger.exists() else {}
    if old:
        price_ledger.validate_ledger(old)
    fetch = fetch or (fixture_fetch(args.fixtures) if args.fixtures else http_fetch)
    outcome = build(old, collect(fetch, model_map), now_iso(), model_map.get("not_priceable", []))
    if args.report:
        args.report.write_text(outcome.report, encoding="utf-8")
    print(outcome.report)
    if args.check:
        return EXIT_SOURCE_FAILED if outcome.failures else (EXIT_OUTDATED if outcome.changed else EXIT_OK)
    if outcome.changed and not outcome.blocked:
        args.ledger.write_text(price_ledger.dumps(outcome.ledger), encoding="utf-8")
    if outcome.failures:
        return EXIT_SOURCE_FAILED
    return EXIT_NEEDS_REVIEW if outcome.review else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(run())
