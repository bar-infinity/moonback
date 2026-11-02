# LUNA Tools / CSV Test Cases

Version: **0.1.0** · Metadata schema: **1.0** · Created: **2026-11-03**

This is a prototype of ten small, synthetic CSV fixtures for humans and
AI agents testing CSV ingestion. It is distributed on GitHub under CC0-1.0 and
can be used from the repository. MoonBack Web routes are not implemented.
It is a test corpus, not an upload service, converter, repair tool,
production validator, or replacement for any paid product.

All item IDs and labels are invented. There are no real people, contact details,
organizations, credentials, customer records, or external datasets. The Japanese,
Greek and French strings are sample item labels, not personal names.

## Files and discovery

Start with [`cases.json`](cases.json). It contains the profile, provenance,
license, case IDs, relative filenames, purposes, features, byte lengths, SHA-256
fingerprints, complete expected decoded rows, expected stage outcomes and issues.
The original fixture bytes are in `samples/`; executable consistency tests are
in `tests/test_cases.py`. No dependency installation is required.

| ID | File | Classification | Expected behavior |
| --- | --- | --- | --- |
| csv-001 | basic-records | valid | Two records; all checks pass |
| csv-002 | quoted-comma | valid | Embedded comma remains in one label |
| csv-003 | escaped-quotes | valid | Doubled quotes become a literal quote |
| csv-004 | multiline-field | valid | Embedded CRLF remains inside one logical record |
| csv-005 | utf8-labels | valid | Non-ASCII synthetic labels decode without loss |
| csv-006 | unclosed-quote | syntax_error | Strict parser raises `csv.Error` at EOF |
| csv-007 | text-after-quote | syntax_error | Strict parser raises `csv.Error` after a closing quote |
| csv-008 | uneven-field-count | data_quality | Parser succeeds; record structure fails; business checks are not run |
| csv-009 | duplicate-record-id | data_quality | Parser and structure succeed; ID uniqueness fails |
| csv-010 | invalid-business-values | data_quality | Parser and structure succeed; required label and quantity rules fail |

Full filenames, including the ID and `.csv`, are specified in the JSON manifest.
All descriptions and expectations use English; fixture text may be multilingual.

## Declared interpretation

CSV is a family of dialects. These expected outcomes apply to the explicitly
declared `luna-csv-comma-utf8-v1` profile, not every program calling itself a CSV
reader. RFC 4180 is an **Informational RFC**, not an Internet Standard. We use its
record-layout and quoting conventions with a declared UTF-8 extension; this is
not a claim of full conformance to its ASCII-oriented ABNF.

- Strict UTF-8, no BOM; comma delimiter; double-quote quote character.
- Embedded quotes use doubled quotes; no escape character; no initial-space
  skipping. Spaces are significant. No dialect or encoding autodetection.
- Record terminators are CRLF. Files include a final physical CRLF; in csv-006
  it still belongs to an unclosed quoted field and does not complete a record.
- Use Python 3.9+ standard-library `csv.reader` with `strict=True`,
  `quoting=csv.QUOTE_MINIMAL`, and `newline=''` when opening files.
- `strict=True` is a parser setting, **not a complete RFC or business validator**.
  Other parsers may accept malformed quotes or normalize line endings. Such a
  result should be recorded against that parser's documented settings.
- Header: `record_id,label,quantity`; every complete record must have three fields.
- IDs match `T[0-9]{3}` and are unique. Labels must not be empty or whitespace-only.
- Quantity matches `0|[1-9][0-9]*`: ASCII nonnegative integer text, no sign,
  decimal point, separators, or leading zeros except the value `0` itself.
- Values remain strings. Do not trim, repair, fill missing fields, or infer types.

The processing stages are **decode → parse → structure → business quality**.
After parsing fails, later stages are `not_evaluated`, not passes. After record
structure fails, business checks are `not_evaluated`. A successful parse does
not imply acceptable data; a business-rule failure does not make the CSV quoting
syntax invalid. csv-008 also violates RFC 4180's uniform-field-count convention,
although Python's strict reader parses its individual records successfully.

Issue locations are **1-based logical record numbers**, counting the header as
record 1, never physical line numbers. A quoted field can contain several lines.
Expected rows include the header; `data_record_count` excludes it. A parse error
has null rows/count; partial parser results are deliberately not an oracle.

## Reproduce the checks

From the MoonBack repository root:

```sh
python3 -m unittest discover -s luna_tools/csv_test_cases/v0.1.0/tests -v
```

Run the existing MoonBack suite from `moonback_mvp/`:

```sh
cd moonback_mvp
python3 -m unittest discover -s tests -v
```

Tests use exact bytes and independently hand-authored expected rows, not parser
output generated on each run. They check the manifest, file coverage, hashes,
newline/encoding properties, parse behavior, structure and business findings.
The Python helpers are test assertions only; they do not expose a new service or
product. Tests never fetch reference URLs or call an external API.

Avoid opening and resaving the fixtures in spreadsheet editors: they may silently
repair quotes, infer types, or change CRLF/UTF-8 bytes. The directory-local
`.gitattributes` disables Git text conversion for these CSV files; fingerprints
intentionally fail if any editor or transport changes their bytes. No
formulas, macros, external links or executable payloads are included in the data.

## Provenance and use

The samples and expected values are original, hand-authored synthetic fixtures
created for MoonBack / LUNA Tools. Reference documents informed the interpretation;
their text, code and datasets were not copied into these fixtures:

- [RFC 4180](https://www.rfc-editor.org/rfc/rfc4180): record layout and quoting.
- [Python CSV documentation](https://docs.python.org/3.9/library/csv.html):
  reference parser behavior and `newline=''` handling.

The original assets in this version directory are made available under
**CC0-1.0**; see [LICENSE.md](LICENSE.md). They may be used in personal, educational,
research and commercial tests. No attribution requirement is added; a useful
optional citation is `MoonBack / LUNA Tools, CSV Test Cases v0.1.0, csv-NNN`.
No warranty or guarantee of compatibility, completeness or regulatory compliance
is made. Do not imply endorsement by MoonBack, LUNA Tools, the IETF or Python.

The license does not relicense the surrounding MoonBack repository, referenced
documents or any paid product. GitHub distribution under CC0-1.0 does not depend
on MoonBack Web publication. Adding MoonBack Web routes requires a separate
supervisor review; Archive/Journal publication approvals remain unchanged.

## Extension and future publication

Do not silently replace a released case's bytes or expected interpretation.
Add stable IDs for new cases; use a new version directory for changed released
fixtures. Keep the JSON schema version separate from the dataset version.

For 20 cases, add declared profiles for BOM, LF-only endings, alternative
delimiters, header-only data, blank records and significant whitespace. For 50,
add explicit schema-bound date/decimal/null/normalization and larger-record cases.
Specify each profile and its stage outcomes; never label a dialect difference
or domain-specific rule a universal CSV error. Review spreadsheet-formula risks
separately before introducing formula-like strings; no such cases are included.

A future versioned URL could be `/tools/csv-test-cases/v0.1.0/`, with `cases.json`
and `samples/{filename}.csv` beneath it. Serve JSON as `application/json` and CSV
as `text/csv; charset=utf-8` with attachment download and `nosniff` headers;
do not interpret fixture content as HTML or add upload/execution features.
No MoonBack Web route, sitemap entry, `agent.json` entry or website deployment
has been added for this dataset. This does not restrict its use from GitHub
under CC0-1.0.
