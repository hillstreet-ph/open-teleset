# Channel & Group Finder diagnostics

The finder answers from public web sources when the TGStat directory is
unavailable. Its job is to return rows the operator can act on, and to say
exactly why a row was not returned — never to blame the keyword for every
empty result.

## What each stage may claim

- **Directory rows** (Lyzem, Telegram Search Engine) are candidates, not
  verdicts. A row can still be a dead link, the wrong country, or the wrong
  kind. A directory that returns a short page therefore does not end the
  search: the generic web race still runs, and only a full page short-circuits
  it.
- **The t.me preview is authoritative metadata.** Title, username and about
  text are read from the live page, then the country, language, category, type
  and keyword decisions are re-applied to that metadata. A row that passed on
  a directory's summary but fails on the live page is dropped and counted.
- **A search engine hitting the keyword is not evidence the page is about the
  keyword.** Generic candidates are accepted only when the page's own title,
  username or about text matches. Telegram Search Engine about text is ad copy
  and is ignored for that decision.

## Reading the counters

`filtered` counts drops per bucket: `type`, `country`, `language`, `category`,
`members`, `keyword`, `dead`. `preview_stats` counts t.me fetches
(`requests`, `ok`, `errors`, `skipped`, `dead`). The operator sees both, plus a
sentence naming every non-zero bucket, for example:

```
Candidates were found but each one was dropped: 4 dead, 1 off-country.
Dead links are gone from Telegram. Clear the country filter to see the rest.
```

The advice list only mentions buckets that actually dropped rows, so a run with
no country drops never suggests clearing the country filter. If nothing dropped
and nothing matched, the message says so plainly and does not invent a cause.

## Tests

`tests/test_channel_discovery_accuracy.py` covers the directory short-circuit,
the preview re-check, the honest generic match and the per-bucket reason text.
`tests/test_channel_scraper.py` keeps the source race and preview budget
covering the same paths.
