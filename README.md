# blockflow-prices

A public list of generation prices for the Kie and PiAPI image and video
models used by the BlockFlow app, and the small tool that keeps it current.

BlockFlow is a local app for generating images and videos with your own Kie
and PiAPI API keys. Neither provider has an API that quotes a price before a
job runs, so BlockFlow estimates the cost from this list. After the job runs,
it shows what the provider actually charged.

The prices are a best-effort snapshot. The charge your provider reports is
always the authoritative number.

## Files

| File | What it is |
| --- | --- |
| `prices.json` | The price list (the "ledger"). |
| `model-map.json` | How each provider's published price rows map to the model identifiers in `prices.json`, plus notes for models that cannot be priced up front. |
| `update_price_ledger.py` | Rebuilds `prices.json` from the providers' public price lists. Standard library only. |
| `price_ledger.py` | The strict validator every reader of `prices.json` uses. |
| `tests/` | Tests, with invented example pages in `tests/fixtures/` that have the same structure as the providers' pages. |

## The format

`prices.json` has `schema_version` 1:

```json
{
  "schema_version": 1,
  "generated_at": "2026-11-15T13:34:29Z",
  "sources": {
    "kie_pricing_api": {"url": "https://kie.ai/pricing", "fetched_at": "2026-11-15T13:34:29Z", "note": "..."}
  },
  "entries": [
    {"provider": "kie", "model": "bytedance/seedance-2", "resolution": "720p", "variant": null,
     "unit": "credits_per_second", "amount": 41.0, "usd": 0.205,
     "source": "kie_pricing_api", "source_row": "bytedance/seedance-2, 720p no video input"}
  ],
  "not_priceable": [
    {"provider": "kie", "model": "gpt-image-2-6/flare-text-to-image", "code": "priced_per_token",
     "message": "Kie prices this model per token, so the cost depends on the output and cannot be estimated up front.",
     "source": "https://kie.ai/pricing"}
  ]
}
```

- **`model`** is the provider's own identifier. For Kie it is the `model`
  string. For PiAPI it is the `task_type`, with `-less-restriction` where that
  applies. PiAPI Kling is written `kling:<version>`, and PiAPI upscaling is
  `upscale`.
- **`resolution`** is lower case (`720p`, `1k`, `2k`) or `null`.
- **`variant`** holds any other billing dimension. Today the only value is
  `video_input`, the per-second rate used when input videos are attached.
- **`unit`**:
  - Kie: `credits_per_second`, `credits_per_image`, `credits_per_extra_image`
    or `credits_per_reference_image`. One Kie credit is $0.005.
  - PiAPI: `usd_per_second`, `usd_per_image`, `usd_per_extra_image` or
    `usd_per_megapixel`.
- **Dates:** an entry's date is its source's `fetched_at`.
- **`not_priceable`** lists models a provider offers but that cannot be priced
  before a run. Each note has a reason code:
  - `priced_per_token`;
  - `input_video_billing_unknown`;
  - `conflicting_sources`;
  - `no_published_price`.

`price_ledger.py` rejects any file that does not follow these rules. That
includes an unknown provider or unit, an amount that is not a positive finite
number, a non-https source URL, unknown fields and duplicate entries.

## Where the numbers come from

- **Kie:** the list behind https://kie.ai/pricing. The page shows only the
  first rows. The rest come from the same public endpoint the page itself
  calls, `POST https://api.kie.ai/client/v1/model-pricing/page`, which the
  updater pages through 100 rows at a time. No API key is used. This endpoint
  is not a documented API, so reading it is unofficial and can break at any
  time.
- **PiAPI:** the price tables in its Markdown docs at
  `https://piapi.ai/docs/<path>.md`.

The updater sends a generic User-Agent, makes about a dozen requests, and uses
timeouts.

## The daily update

`.github/workflows/update-prices.yml` runs `update_price_ledger.py` every day,
and can also be started by hand.

- **No change:** nothing happens.
- **Prices changed:** it opens or updates one pull request from the
  `automation/price-ledger` branch. The body is a report listing prices
  added, removed and changed (old → new, % change), and provider rows that
  look relevant but are not mapped yet. Review it and merge.
- **A source failed:** if a source could not be read or returned far fewer
  prices than expected, its previous prices are kept and the run opens or
  updates one issue titled "Price ledger update needs attention".
- **A big jump:** any price that moved by more than ×2 or ÷2 opens the same
  issue for review.
- **Auto-merge:** pull requests are never merged automatically unless the
  repository variable `PRICE_LEDGER_AUTO_MERGE` is set to `true`, and even
  then only for runs with nothing flagged. Auto-merge must also be enabled in
  the repository settings.

Pull requests opened with the workflow's own token do not start other
workflows, so CI does not run on them automatically. Start it by hand if you
need it.

## How BlockFlow uses this file

BlockFlow bundles a copy of `prices.json` with each release. It can also
download the latest version from
`https://raw.githubusercontent.com/Hearmeman24/blockflow-prices/main/prices.json`,
at most every 12 hours. That download is a single GET request for this static
file and sends nothing about the user, their API keys, their prompts or their
jobs. Users can turn it off in BlockFlow's settings.

When a finished job's real charge differs from this list, BlockFlow can offer
a prefilled "report a price change" link. It contains only:

- provider;
- provider model identifier;
- variant;
- unit;
- listed price and its date;
- observed price and its date.

Nothing is sent unless the user opens the link and submits the issue.

## Reporting a wrong price

Open an issue with the "Report a price change" form. Include the provider, the
model identifier as it appears in `prices.json`, the variant, the unit, the
listed price and what you were charged. Do not include API keys, prompts or
account details.

## Adding a model

1. Run the updater and read the report:

   ```sh
   python3 update_price_ledger.py --check --report report.md
   ```

   New provider rows that look relevant are listed under "Unmapped provider
   rows".
2. Add a mapping to `model-map.json`:
   - **Kie:** add an entry to `kie.rows`. Give the exact row text as
     `description` (case and spacing are ignored), a `unit`, and one or more
     `targets` (`{"model", "resolution", "variant"}`). One row can feed several
     targets, for example a shared "1K / 2K" price.
   - **PiAPI:** add a doc under `piapi.docs`:
     - Its `tables` are read as rows × columns from the first Markdown table
       after the `after` text. Labels are lower-cased with spaces removed.
     - Its `patterns` are regular expressions with one group, the price.
3. If a model cannot be priced up front, add a `not_priceable` note instead.
   Give the provider, the model, an optional variant, a reason code, a
   one-sentence message and the source URL. The updater copies the notes into
   `prices.json`, and its report says when a noted model gets a price.
4. Run the updater, then the tests, and open a pull request with both
   changed JSON files.

BlockFlow also has to know how a new model is billed (per second, per image,
surcharges) before it can show an estimate. That part lives in the app.

## Running it locally

You need Python 3.12 or newer. The updater uses only the standard library.

```sh
python3 update_price_ledger.py                     # rewrite prices.json if anything changed
python3 update_price_ledger.py --check             # exit 1 if prices.json is out of date
python3 update_price_ledger.py --report report.md  # also write the Markdown report```

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Done, or already up to date. |
| 1 | With `--check`: the file is out of date. |
| 2 | A source failed; its previous prices were kept. Takes precedence over 3. |
| 3 | A price moved by more than ×2 or ÷2; it was written and needs review. |

Tests and lint (with [uv](https://docs.astral.sh/uv/), or install `pytest` and
`ruff` any other way):

```sh
uv run --with pytest pytest -q
uvx ruff check .
```

The tests use the saved pages and never fetch from the providers.
