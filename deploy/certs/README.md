# Supabase database trust anchor

`supabase-prod-ca-2021.crt` is a public CA certificate, not a credential.
Downloaded from the certificate link in the canonical project's Database
Settings on 2026-10-10:

https://supabase-downloads.s3-ap-southeast-1.amazonaws.com/prod/ssl/prod-ca-2021.crt

SHA-256 certificate fingerprint:
`807025ad50d4ed219d2c9c7d299c004f824eb00cf7f65afef607d07b72e6cafa`

Expires 2031-04-26. Verify replacements through the Supabase dashboard before
updating this trust anchor. The image and CI configure `PGSSLROOTCERT`; production
connection secrets use `sslmode=verify-full`. Never disable certificate or
hostname verification to repair connectivity. See
https://supabase.com/docs/guides/platform/ssl-enforcement.
