# Kijiji fixtures

Real captured responses from kijiji.ca, captured **2026-05-30**. Kijiji is a
Next.js + Apollo app: all listing data lives in the `__NEXT_DATA__` `<script>`
JSON (`props.pageProps.__APOLLO_STATE__`), keyed `AutosListing:<id>`. Parse that
JSON — do not scrape the DOM.

## Files

| File | Source URL | Shape |
| --- | --- | --- |
| `search_owner_alberta.html` | `/b-cars-trucks/alberta/c174l9003?for-sale-by=ownr` | 45 owner listings, 0 dealers — the production search URL |
| `search_mixed_alberta.html` | `/b-cars-trucks/alberta/c174l9003` (unfiltered) | 39 dealers + 7 owners — exercises the dealer-skip path |
| `listing_detail_jeep_cherokee.html` | `/v-cars-trucks/.../1738329373` | Happy path: full title/description, 15 photos |
| `listing_detail_sparse.html` | `/v-cars-trucks/.../1736878962` | Degenerate: title `"Hon"`, $99,999,999 price, ~empty description, 1 photo |

## Search URL pattern

`https://www.kijiji.ca/b-cars-trucks/<province-slug>/c174l<locationId>?for-sale-by=ownr`

- `c174` = cars & trucks category; `l<id>` = location. Alberta = `l9003`.
- `?for-sale-by=ownr` is the only owner filter that works (`=owner` → 0 results;
  the `/for-sale-by-owner` path returns dealers). Even so, promoted dealer ads
  can leak onto owner pages — skip any `posterInfo.posterId` starting
  `COMMERCIAL` as defense in depth (`search_mixed_alberta` covers this).

## `AutosListing` → `RawPrivateListing` field mapping

| Apollo field | RawPrivateListing | Notes |
| --- | --- | --- |
| `id` | `source_listing_id` | |
| `url` | `url` | `/v-cars-trucks/<city>/<slug>/<id>` |
| `title` | `title` | **Authoritative source for year/make/model/trim** |
| `description` | `description` | Truncated on search page; full on detail page |
| `imageUrls` | `photos` | 5 on search page; all (≤N) on detail page |
| `price.amount` | `ask_price_cad` | **In cents** — divide by 100; `surcharges` may be `PLUS_GST` |
| `location.name` | `pickup_city` | |
| `location.address` | `pickup_province` | Parse the `, AB ` / `, SK ` / `, MB ` token |
| `attributes.all[]` | year/make/model/trim/mileage | `canonicalName` → `canonicalValues`; see caveat |

### Parser caveats (from the captured data)

- **Attributes are sparse for owner listings.** The Jeep fixture has
  `caryear=[]`, `cartrim=[]`, `carmileageinkms=[]` even though make/model are
  present. Derive year/model/trim from the **title** and fall back to attributes,
  not the reverse.
- **VIN** is carried in a structured `vin` attribute on the **search** page for
  the subset of sellers who entered one (~9/45 owners, ~39/46 on the mixed
  page); the detail page has no attributes. Read it when present; the enricher /
  `find_carfax_url` still backfills from the description for the rest.
- **Two-stage scrape is warranted**: the search stub has a truncated description
  and only 5 photos; the detail page carries the full description and all photos.

## Re-capturing

```
curl -A '<browser UA>' '<url>' > <fixture>.html
```
Plain `curl` with a browser User-Agent suffices — no headless browser or auth
needed as of the capture date. If kijiji.ca re-platforms off Next.js, the
`__NEXT_DATA__` extraction is what breaks; re-capture and re-inspect the JSON.
