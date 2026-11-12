# HAR Budget Audit

A small **offline**, Python-standard-library command-line checker for request-count and response-transfer budgets. It emits only fixed-schema aggregate counts and sizes. It never contacts the recorded sites, uploads the HAR, or includes URLs, hosts, headers, cookies, bodies, MIME strings, timestamps, or file paths in its reports.

Useful when comparing a browser capture against a page-load budget without attaching a sensitive HAR to a ticket. Original implementation; inspired by the emphasis on reproducible performance evidence in [Astryx](https://github.com/meta/astryx), not affiliated with it. This is not the first HAR analysis tool; for rich visual analysis, use browser developer tools or a full HAR viewer.

## Run

Requires Python 3.10+ (tested on Linux, Python 3.12.14; other versions/platforms not tested). No packages or installation needed.

```sh
python har_budget.py example.har --max-transfer-bytes 500 --max-requests 3
python har_budget.py capture.har --max-transfer-bytes 2000000 --max-requests 80
python -m unittest -v
```

The included `example.har` is entirely synthetic and contains no real browsing data.

Exit codes:
- `0`: all requested budgets pass; or no budgets were requested (`not_requested`, **not** a budget pass).
- `1`: at least one budget fails.
- `2`: a budget cannot be established because sizes are unknown, or input/arguments are invalid. Check the JSON `status` to distinguish `unknown` from `invalid`.

Budgets are inclusive. Exactly 500 bytes passes a 500-byte budget. With multiple budgets, a known failure takes precedence over unknown data.

## Size semantics

For each entry, response transfer bytes come from a nonnegative `response._transferSize` when present. This common browser extension is treated as the exporter's reported response transfer size, including its reported overhead, **not independently measured wire traffic**. If absent or `-1`, the fallback is `response.headersSize + response.bodySize` when both are known. This does not include request upload bytes, TLS/TCP overhead, redirects missing from the capture, or traffic outside this HAR.

If only one standard component is known, that component contributes to `known_total`, but the entry remains unknown. Thus `known_total` is a **lower bound**, not an exact total when `unknown_entries > 0`. Known bytes over budget fail even with unknown entries; known bytes below budget with unknown entries are `unknown`, never pass. `standard_entries` counts fully known standard totals; `extension_entries` counts totals taken from `_transferSize`.

Decoded content (`response.content.size`) is counted separately. Compression can make decoded size much larger than transfer size. Unknown decoded size does not invalidate a transfer budget. Missing byte fields and integer `-1` mean unknown. Zero means known zero; strings, booleans, floats, other negative values and values above 2^63-1 are invalid. Even size fields unused because an extension takes precedence are validated.

### Cache, duplicates and capture scope

- Each HAR entry counts as one request, including repeated URLs and redirects. Entries are never deduplicated: repeats can represent real repeated work.
- Cache metadata and HTTP 304 do not imply zero transfer. A 304 response may transfer headers. An explicitly reported `_transferSize: 0` is accepted as zero, while missing sizes stay unknown.
- `responses_304` is a simple count, **not a cache-hit rate**.
- All entries are aggregated, including multiple HAR pages, warm-cache requests and background requests. Isolate the desired page/capture window before export. The tool cannot prove the capture is complete, a cold load, or representative of a physical phone/WAN.
- No load-time, memory, GPU, responsiveness, accessibility or security conclusion is made.

## Privacy and validation boundaries

Reports have fixed field names and numeric counts/sizes/status strings only. Parse/validation errors use fixed messages, with no source snippets or path echo. JSON duplicate keys and nonstandard NaN/Infinity are rejected. At most 32 MiB is read, with a 100,000-entry limit and graceful malformed/deep-JSON errors. Only regular files are accepted; nonblocking opens are used where supported. It is not a complete HAR specification validator and ignores unrelated fields. Input is read-only; stdout/stderr are the only outputs.

**A HAR may contain credentials and personal information.** Keep the source file private. Aggregate totals are not a formal anonymization guarantee: unusual counts or sizes may still be sensitive. Review a report before sharing it. This utility does not sanitize or produce a shareable HAR,.

## Development

`python -m unittest -v` covers transfer vs decoded sizes, unknown lower bounds, browser extensions, cache/revalidation, duplicate requests, inclusive budgets, malformed shapes/types, input limits and non-disclosure. `python -m py_compile har_budget.py test_har_budget.py` checks syntax. No hosted CI or real-user HAR measurement is claimed by this initial release.

MIT licensed. Contributions should include a synthetic regression fixture or test; never submit private captures or secrets.
