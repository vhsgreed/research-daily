# research-daily

A scheduled research brief: RSS feeds in, one model-written email out. It has four sections: AI/frontier, geopolitics, markets/macro, and **"same story, different angles"**. The angles section compares how outlets from different blocs frame the same event: BBC, Guardian and DW against TASS, CGTN, Press TV, Anadolu, Al Jazeera, The Hindu, Dawn and SCMP.

Personal tool, published as is. No support, no roadmap.

## What is worth taking from it

1. **Cross-bloc grouping in code, not in the prompt.** Ask a model to find "the same story across outlets" in a flat headline list and it fails both ways. It misses real matches and pairs unrelated stories, even when told never to pad. Here the headlines are embedded locally (`nomic-embed-text`), grouped by average-linkage clustering at cosine 0.84, and only groups containing both a Western and a non-Western outlet go to the model. The model then compares framing inside verified groups. A post-check drops any comparison that still lacks both blocs. See `skills/cross-bloc-angles/SKILL.md`.
2. **Dedupe against what was already sent.** Each section drops items whose URL or title appeared in the last 7 days of archived briefs. It also drops feed items older than 4 days, because slow feeds keep serving month-old entries. The model gets the previous 3 briefs as "already told the reader" so it does not rewrite the same themes.
3. **Fail-soft stages.** A dead feed, a failed model call or a missing indexer never aborts the run. Problems are listed inside the delivered email.

## Cost and fallback chain

| Stage | Default | Cost |
|---|---|---|
| Fetch | stdlib `urllib` + `xml.etree` (RSS 2.0, Atom, RDF) | $0 |
| Write | `inclusionai/ling-3.0-flash-vl` on OpenRouter, reasoning **off** | ~$0.0005 per run |
| Fallback writer | `gemma4:26b` on local Ollama | $0 |
| Last resort | raw headline list | $0 |
| Grouping | `nomic-embed-text` on local Ollama | $0 |
| Send | `himalaya message send`, `multipart/alternative` | $0 |

Leave Ling's reasoning off. With it on, the provider returned the chain of thought inside `content`, and the call hit `max_tokens` before writing the brief.

## Run

Python 3.10+, stdlib only. Ollama with `gemma4:26b` and `nomic-embed-text` pulled. `himalaya` configured with an SMTP account.

```bash
export RD_MAIL_TO=you@example.com
export RD_MAIL_FROM=sender@example.com          # the address your SMTP relay owns
export RD_ENV_FILE=~/.config/research-daily.env # contains OPENROUTER_API_KEY=...

python3 research-daily.py --dry-run             # writes to $RD_DIR/.dry-run/, sends nothing
python3 research-daily.py --beats angles --dry-run
python3 research-daily.py --no-llm --dry-run    # raw lists, no model at all
python3 research-daily.py                       # full run, sends
```

| Variable | Default | Purpose |
|---|---|---|
| `RD_MAIL_TO` / `RD_MAIL_FROM` | (none) | Recipient and sender |
| `RD_DIR` | `~/research` | Archive of dated briefs, which is also the dedupe backlog |
| `RD_ENV_FILE` | (none) | Dotenv file holding `OPENROUTER_API_KEY` (cron has no shell env) |
| `RD_CLOUD` | `1` | `0` = local model only |
| `RD_OLLAMA` | `http://127.0.0.1:11434` | Ollama base URL |
| `RD_LOCAL_MODEL` | `gemma4:26b` | Fallback writer |
| `RD_HIMALAYA` | `which himalaya` | Absolute path; cron's PATH usually lacks `~/.local/bin` |
| `RD_READER` | `an independent founder` | Who the brief is written for |
| `RD_SWEEP` / `RD_INDEX` | `~/src/research-sweep/…`, `~/src/research-index/…` | Optional: [research-sweep](https://github.com/vhsgreed/research-sweep) adds search results; [research-index](https://github.com/vhsgreed/research-index) keeps the archive full-text searchable |

Cron: use absolute paths for both `python3` and the script, and append the output to a log.

## Known weaknesses

- The framing analysis rests on headlines plus a 200-character summary. The model never reads the articles, so "outlet X omits Y" can just mean a short headline.
- The 0.84 clustering threshold was tuned on one day (249 headlines). On a busy day it may merge unrelated stories, and on a quiet one it may split real matches.
- The bloc split is crude. "Non-Western" lumps adversarial state media together with independent outlets (The Hindu, Dawn, SCMP). Russian and Chinese state media mostly cover stories the Western outlets do not, so many days have no state-media comparison.
- The backlog records only the first 40 links per brief, so unlisted items can come back as "new".
- RT and Sputnik are refused by many EU DNS resolvers. Global Times and Xinhua had stale feeds when tested.

## Why the author stopped using it

Aggregating news that is already aggregated adds little without data you can't get from public feeds: a terminal, paid wires or primary-source OSINT. The grouping and dedupe parts are the reusable pieces.

## License

MIT
