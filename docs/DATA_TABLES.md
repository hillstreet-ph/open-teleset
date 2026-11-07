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
| `POST /api/tables` | Create a table |
| `GET /api/tables/{id}` | Read the full sheet |
| `PATCH /api/tables/{id}` | Rename |
| `DELETE /api/tables/{id}` | Delete |
| `POST /api/tables/{id}/columns` | Add a column |
| `PATCH /api/tables/{id}/columns/{column_id}` | Rename or retype a column |
| `DELETE /api/tables/{id}/columns/{column_id}` | Delete a column |
| `POST /api/tables/{id}/rows` | Add a row |
| `PATCH /api/tables/{id}/rows/{row_id}` | Update cells |
| `DELETE /api/tables/{id}/rows/{row_id}` | Delete a row |

Capacity and duplicate-column violations return `409`; invalid values and
unknown types return `400`. Mutations return the updated table so the dashboard
can refresh without a second request.

## Verification

`tests/test_tables.py` covers storage, operator isolation, typed values, the
credential refusal, bounds, encryption at rest and the missing-key failure.
`tests/test_tables_ui.mjs` drives the dashboard methods against a fake HTTP
client. `ui_account_tables.mjs` in the workspace proves the real page in Chrome:
create a table, add date and checkbox columns, type a row, reload and confirm
persistence, watch a credential-shaped cell be refused, then delete a column and
the table.

## Rollback

Revert the commit. Existing encrypted table records remain in the operator store
and are ignored by the previous release; deleting `accounts/tables.secret.json`
removes them.