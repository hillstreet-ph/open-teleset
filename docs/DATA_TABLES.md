# Data tables

Data tables are small, operator-owned sheets for lists that outgrow a note:
suppliers and delivery windows, leads and follow-ups, a roster with dates. A
table has named columns of `text`, `number`, `date` or `checkbox` cells and rows
the operator edits directly in the dashboard at **Data Tables**.

## Bounds

| Bound | Value |
| --- | --- |
| Tables per operator | 30 |
| Columns per table | 20 |
| Rows per table | 2000 |
| Cell length | 500 characters |
| Table and column names | 80 characters |

A new table starts with one `Name` text column. The last column cannot be
deleted; delete the table instead. Column type changes re-validate every saved
cell first, so a text column cannot become a number column while it holds prose.

## Contact master list

The **Start from** chooser on the create form offers a *Contact master list*
preset: a table named **Contacts** with `Username`, `Name`, `Status` and `Notes`
text columns, ready to hold the people you intend to message. A blank table is
still the default; the preset only saves the column setup.

## Import

**Import** on an open table accepts a file (`.csv`, `.tsv`, `.txt`, `.json`) or
pasted text, in the Airtable/Sheets shape: a header row plus one row per record.

- **Format** detects CSV, TSV or JSON automatically, or you can pin one. JSON is
  either a list of `{"Column": value}` objects or
  `{"columns": [...], "rows": [[...], ...]}`.
- Headers map to existing columns by name (case-insensitive). A header the table
  does not have becomes a new **text** column, up to the column cap.
- **Append rows** keeps what is there; **Replace all rows** clears the rows
  first. Columns are never replaced.
- Values are coerced to the column type: numbers parse, `yes/no/true/false/on/off`
  (and `1/0`) become checkboxes, and `YYYY/M/D` dates normalise to ISO. A value
  that does not fit is refused with the row and column named; nothing is
  partially imported.
- The payload is capped at 600,000 characters (`413` beyond that) and the table
  at 2000 rows (`409` when an append would exceed it). Credential-shaped cells
  are refused exactly as in manual editing.

## Export

**Export CSV** downloads a sheet-shaped file (header row, `true`/`false`
checkboxes, blank for empty cells) named after the table. **Export JSON**
downloads `{"name", "columns": [{"name", "type"}], "rows": [{"Column": value}]}`,
which re-imports into a fresh table with the same column names.

## Sending selected rows to BulkSend

Rows have checkboxes, a search filter and **Select all**. Pick which text column
holds the contact (`Username` by default on the preset), then **Send selected to
BulkSend** merges the deduplicated, `@`-stripped, lowercased values into the
BulkSend target list and opens that page. It only prefills: per-contact messages
are cleared, **consent stays unchecked**, and the normal consent-gated send
applies. Nothing is sent from the table itself. **Delete selected** removes the
chosen rows after the usual confirmation.

## Values

- **Text** is stored as typed. NUL characters are rejected.
- **Number** accepts integers and decimals (including exponent form) and stores
  a real number. Booleans and non-finite values are rejected.
- **Date** accepts ISO `YYYY-MM-DD`.
- **Checkbox** stores a boolean; a blank cell is `false`.

Blank values are stored as `null` rather than an empty string, so an untouched
cell stays distinguishable from one the operator cleared.

## Credentials are refused, not masked

A cell that looks like a Telegram session string or a base64 key is rejected
with a message pointing the operator to the account setup. Silently masking or
truncating a spreadsheet cell would corrupt working data, so the server refuses
the write and the dashboard restores the previous value. Data tables are for
working lists, not for storing credentials.

## Storage

Tables live in the operator store under the `tables` key and are encrypted with
the same cipher as Telegram sessions, scoped to the authenticated operator who
created them. The file on disk (`accounts/tables.secret.json` by default) holds
a ciphertext blob only and is written atomically with mode `0600`. Without
`SESSION_ENCRYPTION_KEY` the routes return `500` rather than store plaintext.

## API

All routes require an authenticated operator and are scoped to that operator.

| Method and path | Purpose |
| --- | --- |
| `GET /api/tables` | List summaries plus capacity |
| `POST /api/tables` | Create a table (`{"name", "preset": "contacts"}` for the master list) |
| `GET /api/tables/{id}` | Read the full sheet |
| `PATCH /api/tables/{id}` | Rename |
| `DELETE /api/tables/{id}` | Delete |
| `POST /api/tables/{id}/columns` | Add a column |
| `PATCH /api/tables/{id}/columns/{column_id}` | Rename or retype a column |
| `DELETE /api/tables/{id}/columns/{column_id}` | Delete a column |
| `POST /api/tables/{id}/import` | Import rows (`content`, `format`, `mode`, optional `column_map`) |
| `GET /api/tables/{id}/export?format=csv\|json` | Download the sheet |
| `POST /api/tables/{id}/rows` | Add a row |
| `PATCH /api/tables/{id}/rows/{row_id}` | Update cells |
| `DELETE /api/tables/{id}/rows/{row_id}` | Delete a row |

Capacity and duplicate-column violations return `409`; invalid values and
unknown types return `400`. Mutations return the updated table so the dashboard
can refresh without a second request.

## Verification

`tests/test_tables.py` covers storage, operator isolation, typed values, the
credential refusal, bounds, encryption at rest, the missing-key failure, the
contacts preset, import mapping/coercion/refusals and caps, the export
round-trip, and the workflow append helper's dedupe.
`tests/test_tables_ui.mjs` drives the dashboard methods against a fake HTTP
client, including selection and the BulkSend prefill keeping consent unchecked.

## Rollback

Revert the commit. Existing encrypted table records remain in the operator store
and are ignored by the previous release; deleting `accounts/tables.secret.json`
removes them.