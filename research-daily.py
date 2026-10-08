#!/usr/bin/env python3
"""research-daily.py — the daily research newsletter: AI, geopolitics, markets.

Cost model: about $0.01/month.
  - fetching  : stdlib urllib (RSS) + optional research-sweep.py
  - writing   : inclusionai/ling-3.0-flash-vl via OpenRouter (reasoning off,
                ~$0.0002 per section, ~$0.01/month), falling back to
                gemma4:26b on the LOCAL Ollama server if OpenRouter fails
  - grouping  : nomic-embed-text on local Ollama (cross-outlet "angles" beat)
  - delivery  : himalaya (any configured SMTP account)

Configuration is by environment variable; see README.md.

Each beat is independent: one dead feed or one failed summary can never take
down the run. Sources that fail are reported in the digest so the archive says
what it is missing.

Usage:
    research-daily.py                 # all beats, send email
    research-daily.py --beats frontier markets
    research-daily.py --dry-run       # write reports + print digest, do not send
    research-daily.py --no-llm        # skip gemma, ship the raw item list
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as html_module
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.message import EmailMessage

import shutil

HOME = os.path.expanduser("~")
RESEARCH_DIR = os.environ.get("RD_DIR", os.path.join(HOME, "research"))
SWEEP = os.environ.get("RD_SWEEP", os.path.join(HOME, "src", "research-sweep", "research-sweep.py"))
INDEX = os.environ.get("RD_INDEX", os.path.join(HOME, "src", "research-index", "research-index.py"))
OLLAMA = os.environ.get("RD_OLLAMA", "http://127.0.0.1:11434") + "/api/chat"
MODEL = os.environ.get("RD_LOCAL_MODEL", "gemma4:26b")
READER = os.environ.get("RD_READER", "an independent founder")
# Primary writer since 2026-10-08 (A/B: same quality as gemma on these prompts,
# 3-5 s per section instead of 70-160 s, 0 x 429 in 6 calls). Reasoning stays
# OFF: with it on, the provider (Novita) leaks the thinking into `content` and
# the run hits max_tokens before the brief is written.
LING_MODEL = "inclusionai/ling-3.0-flash-vl"
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
USE_CLOUD = os.environ.get("RD_CLOUD", "1") != "0"
WRITER = {"name": ""}   # which model wrote the last section, for the report header


def _openrouter_key() -> str | None:
    """OPENROUTER_API_KEY from the environment, else from the dotenv file named
    by RD_ENV_FILE. cron runs with an empty environment, so the file route is
    what makes a scheduled run find the key."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    path = os.environ.get("RD_ENV_FILE")
    if path:
        try:
            for line in open(os.path.expanduser(path), encoding="utf-8"):
                if line.startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") or None
        except OSError:
            pass
    return None
MAIL_TO = os.environ.get("RD_MAIL_TO", "")
MAIL_FROM = os.environ.get("RD_MAIL_FROM", "")
# Absolute path: cron runs with a minimal PATH that usually lacks ~/.local/bin.
# A bare `himalaya` then works interactively and silently fails from crontab.
HIMALAYA = (os.environ.get("RD_HIMALAYA") or shutil.which("himalaya")
            or os.path.join(HOME, ".local", "bin", "himalaya"))
# Send from the address that owns the SMTP relay. A From: on a domain whose
# SPF/DKIM the relay does not satisfy fails DMARC, and bulk digest traffic on a
# domain you also use for real correspondence costs that domain inbox placement.
UA = {"User-Agent": "research-daily/1.0 (+https://github.com/vhsgreed/research-daily)"}

# kind -> (label, rss feeds, sweep queries)
BEATS = {
    "frontier": {
        "label": "AI / frontier",
        "rss": [
            "https://www.theregister.com/software/ai_ml/headlines.atom",
            "https://arxiv.org/rss/cs.AI",
        ],
        "sweep": [
            "AI agents", "model release", "alignment", "AI regulation",
            "LLM", "AI safety",
        ],
    },
    "geopolitics": {
        "label": "Geopolitics",
        "rss": [
            "http://feeds.bbci.co.uk/news/world/rss.xml",
            "https://www.theguardian.com/world/rss",
            "https://www.aljazeera.com/xml/rss/all.xml",
            "https://rss.dw.com/rdf/rss-en-all",
        ],
        "sweep": ["sanctions", "NATO", "trade war", "conflict escalation"],
    },
    "markets": {
        "label": "Markets / macro",
        "rss": [
            "https://www.cnbc.com/id/100003114/device/rss/rss.html",
            # MarketWatch top stories swapped out 2026-10-08: too much
            # personal-finance filler ("I'm a 68-year-old widow...").
            "https://feeds.bloomberg.com/economics/news.rss",
            # Yahoo's rssindex went 404 (2026-10-08). Replaced with an
            # aggregator for breadth plus two dense wire-grade feeds.
            # Bloomberg/FT articles are paywalled; headlines are not.
            "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
            "https://feeds.bloomberg.com/markets/news.rss",
            "https://www.ft.com/markets?format=rss",
        ],
        "sweep": ["interest rates", "inflation", "central bank", "market selloff"],
    },
    # Same events, different framing. Entries are (outlet, url) so the model
    # can attribute each headline. RT and Sputnik are EU-sanctioned and many
    # EU resolvers refuse them; TASS carries the Russian state line instead. Global Times and Xinhua feeds are stale (Aug 2026 and
    # 2018), so CGTN carries the Chinese state line.
    "angles": {
        "label": "Same story, different angles",
        "mode": "angles",
        "per_feed": 25,
        "rss": [
            ("BBC (UK)", "http://feeds.bbci.co.uk/news/world/rss.xml"),
            ("TASS (Russia, state)", "https://tass.com/rss/v2.xml"),
            ("CGTN (China, state)", "https://www.cgtn.com/subscribe/rss/section/world.xml"),
            ("Guardian (UK)", "https://www.theguardian.com/world/rss"),
            ("Press TV (Iran, state)", "https://www.presstv.ir/rss.xml"),
            ("Al Jazeera (Qatar)", "https://www.aljazeera.com/xml/rss/all.xml"),
            ("DW (Germany, public)", "https://rss.dw.com/rdf/rss-en-all"),
            ("Anadolu (Turkey, state)", "https://www.aa.com.tr/en/rss/default?cat=world"),
            ("The Hindu (India)", "https://www.thehindu.com/news/international/feeder/default.rss"),
            ("Dawn (Pakistan)", "https://www.dawn.com/feeds/world"),
            ("SCMP (Hong Kong)", "https://www.scmp.com/rss/91/feed"),
        ],
        "sweep": [],
    },
}

ANGLES_INSTRUCTIONS = (
    "The headlines below were grouped IN CODE by semantic similarity. Every GROUP is one event "
    "already verified to be covered by at least one Western outlet (BBC, Guardian, DW) and at "
    "least one non-Western outlet. Write a markdown section with:\n"
    "1. **Same story, different angles** — choose up to 4 groups that are POLITICAL (war, "
    "diplomacy, sanctions, elections, trade, state power, protest); skip groups about crime, "
    "celebrities, obituaries, weather or disease unless a government is a party to the story. "
    "Prefer groups containing Russian, Chinese or Iranian state media. For each: a ### heading "
    "naming the event, one line on the bare facts all outlets agree on, then one bullet per "
    "outlet in the group: how it frames the event, quoting its headline wording, and what it "
    "stresses or leaves out compared with the others. Use only headlines inside that group.\n"
    "2. **Only one side is covering** — 2 to 4 bullets picked from the SINGLE-BLOC list: "
    "stories only one bloc carries today, naming the outlet. Prefer state-media stories.\n\n"
    "Describe framing; do not judge which side is right. Never invent coverage that is not in the list."
)



# Novelty against the backlog of sent briefs. Measured 2026-10-08: frontier
# re-sent 23 of 24 links from earlier days, and gemma re-wrote the same
# "What matters" bullets four mornings running.
MAX_AGE_DAYS = 4          # drop feed items older than this (by pubDate)
BACKLOG_DAYS = 7          # links/titles in briefs from this many days are "seen"
BACKLOG_BRIEFS = 3        # previous briefs shown to gemma as "already told the reader"


def _age_days(stamp: str) -> float | None:
    if not stamp:
        return None
    from email.utils import parsedate_to_datetime
    when = None
    try:
        when = parsedate_to_datetime(stamp)
    except Exception:
        try:
            when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except Exception:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 86400


def _title_key(title: str) -> str:
    return re.sub(r"\W+", "", title.lower())[:80]


def load_backlog(beat: str, today: dt.date) -> dict:
    """What earlier briefs for this beat already sent: urls, title keys, brief text."""
    urls: set[str] = set()
    keys: set[str] = set()
    briefs: list[tuple[str, str]] = []
    for back in range(1, BACKLOG_DAYS + 1):
        day = (today - dt.timedelta(days=back)).isoformat()
        path = os.path.join(RESEARCH_DIR, f"{beat}-{day}.md")
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        body, _, tail = text.rpartition("\n## Sources")
        for title, url in re.findall(r"^\s*-\s+\[(.+?)\]\((https?://[^\s)]+)\)", tail, flags=re.M):
            urls.add(url)
            keys.add(_title_key(title))
        if len(briefs) < BACKLOG_BRIEFS:
            # Only the model-written part: drop the generated header lines and
            # the raw fallback list, which carries no framing worth avoiding.
            brief = re.sub(r"\A#[^\n]*\n(\s*\*Generated[^\n]*\n)?(\s*Sources:[^\n]*\n)?", "", body).strip()
            brief = brief.rstrip("-").strip()
            if brief and "raw items (gemma unavailable)" not in brief:
                briefs.append((day, brief[:1800]))
    return {"urls": urls, "keys": keys, "briefs": briefs}


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def http_get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_rss(url: str, limit: int = 12, outlet: str = "") -> tuple[list[dict], str | None]:
    """Return (items, error). Never raises."""
    try:
        raw = http_get(url)
        root = ET.fromstring(raw)
    except Exception as e:
        return [], f"{url} -> {type(e).__name__}: {e}"

    items: list[dict] = []
    # RSS 2.0 and Atom both live in the wild here; handle both shapes.
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag not in ("item", "entry"):
            continue
        title = link = summary = published = ""
        for child in node:
            ctag = child.tag.rsplit("}", 1)[-1].lower()
            text = (child.text or "").strip()
            if ctag == "title":
                title = text
            elif ctag == "link":
                link = text or (child.get("href") or "")
            elif ctag in ("description", "summary"):
                summary = text
            elif ctag in ("pubdate", "published", "updated", "date") and not published:
                published = text
        if title:
            age = _age_days(published)
            # Slow feeds (The Register's AI atom) keep serving month-old pieces;
            # without this cut they get re-briefed every single morning.
            if age is not None and age > MAX_AGE_DAYS:
                continue
            items.append({"title": title, "url": link, "summary": summary, "outlet": outlet})
        if len(items) >= limit:
            break
    return items, (None if items else f"{url} -> 0 items parsed")


def fetch_sweep(kind: str, queries: list[str], out_path: str, days: int = 2) -> tuple[list[dict], str | None]:
    if not os.path.exists(SWEEP):
        return [], f"research-sweep.py missing at {SWEEP}"
    try:
        subprocess.run(
            [sys.executable, SWEEP, kind, *queries, "--days", str(days), "--out", out_path],
            capture_output=True, text=True, timeout=180, check=False,
        )
        if not os.path.exists(out_path):
            return [], "research-sweep.py produced no output file"
        items = []
        for line in open(out_path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"\*?\s*\[?(.+?)\]?\s*\((https?://\S+)\)", line)
            if m:
                items.append({"title": m.group(1), "url": m.group(2), "summary": ""})
            else:
                items.append({"title": line[:160], "url": "", "summary": ""})
        return items, (None if items else "research-sweep.py returned 0 items")
    except Exception as e:
        return [], f"research-sweep.py -> {type(e).__name__}: {e}"


def ling_write(prompt: str) -> str | None:
    """One brief from Ling on OpenRouter. None on any failure -> caller uses gemma.

    429 and 5xx are upstream congestion: retried with backoff, then given up.
    """
    key = _openrouter_key()
    if not key:
        log("  ling: no OPENROUTER_API_KEY; using gemma")
        return None
    body = json.dumps({
        "model": LING_MODEL, "temperature": 0.3, "max_tokens": 4000,
        "reasoning": {"enabled": False},
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    for attempt in range(3):
        req = urllib.request.Request(OPENROUTER, data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {key}",
            "X-Title": "research-daily", **UA})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
            choice = data["choices"][0]
            content = (choice["message"].get("content") or "").strip()
            cost = (data.get("usage") or {}).get("cost")
            log(f"  ling: {len(content)} chars (finish={choice.get('finish_reason')}, "
                f"provider={data.get('provider')}, cost=${cost})")
            if choice.get("finish_reason") != "stop" or len(content) < 400:
                log("  ling output truncated or too short; using gemma")
                return None
            return content
        except urllib.error.HTTPError as e:
            log(f"  ling HTTP {e.code} (attempt {attempt + 1})")
            if e.code == 429 or e.code >= 500:
                time.sleep(10 * 2 ** attempt)
                continue
            return None
        except Exception as e:
            log(f"  ling failed: {type(e).__name__}: {e}")
            return None
    log("  ling: upstream congested (429/5xx x3); using gemma")
    return None


def gemma_summarise(beat_label: str, items: list[dict],
                    previous: list[tuple[str, str]] | None = None,
                    instructions: str | None = None, max_items: int = 30,
                    cloud: bool = True, listing: str | None = None) -> str | None:
    """Ask the LOCAL model for a tight brief. Returns None if Ollama is unusable.

    `previous` holds (date, brief) pairs already sent; the model is told not to
    repeat them and to frame continuing stories as what changed.
    """
    if not items:
        return None
    listing = listing or "\n".join(
        f"- " + (f"[{i['outlet']}] " if i.get("outlet") else "") + f"{i['title']}"
        + (f" ({i['url']})" if i["url"] else "")
        + (f"\n  {i['summary'][:200]}" if i["summary"] else "")
        for i in items[:max_items]
    )
    history = ""
    if previous:
        history = (
            "ALREADY SENT TO THE READER (earlier briefs, newest first). Do NOT repeat "
            "these points, themes or 'worth watching' trends. If a headline below "
            "continues a story NAMED in the text below, write only what is new, "
            "prefixed 'Update:'. Never use 'Update:' for a story absent from that "
            "text. Pick a different angle for the one-line summary than any "
            "of these used.\n\n"
            + "\n\n".join(f"--- {d} ---\n{b}" for d, b in previous)
            + "\n\n"
        )
    prompt = (
        f"You are writing one section of a daily research brief for {READER} "
        f"who tracks {beat_label}. Below are today's raw headlines, already filtered to "
        f"items the reader has not seen in earlier briefs.\n\n"
        + (instructions + "\n\n" if instructions else
        f"Write a markdown section with:\n"
        f"1. A one-line summary of the day's signal.\n"
        f"2. **What matters** — 3 to 5 bullets, each one sentence, each with the reason it matters.\n"
        f"3. **Worth watching** — 1 to 2 lines on anything that looks like the start of a trend.\n\n") +
        f"Be concrete and specific. No filler, no hedging, no 'in today's fast-paced world'. "
        f"Do not invent facts not present in the list. Fewer bullets beats recycled ones. "
        f"Use only ## and ### headings, never a single #. Output markdown only.\n\n"
        f"{history}"
        f"TODAY'S NEW HEADLINES:\n{listing}"
    )
    if cloud and USE_CLOUD:
        content = ling_write(prompt)
        if content:
            WRITER["name"] = f"{LING_MODEL} (OpenRouter)"
            return content
    WRITER["name"] = "gemma4:26b (local Ollama)"
    payload = json.dumps({
        "model": MODEL, "stream": False,
        # Summarisation does not need chain-of-thought. With thinking left on,
        # gemma burns its token budget in `thinking` and returns an EMPTY
        # `content`, which reads as total failure. Ask for the direct answer.
        "think": False,
        # Without num_predict Ollama picks a small default and the brief gets cut
        # off mid-sentence. Without num_ctx the CONTEXT is Ollama's small default
        # too, so a long headline list fills the window and the model is left with
        # a handful of tokens to answer in (observed: eval=28, done=length).
        "options": {"num_predict": 2048, "num_ctx": 16384, "temperature": 0.3},
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    try:
        req = urllib.request.Request(
            OLLAMA, data=payload,
            headers={"Content-Type": "application/json", **UA},
        )
        with urllib.request.urlopen(req, timeout=420) as r:
            data = json.loads(r.read())
        msg = data.get("message") or {}
        content = (msg.get("content") or "").strip()
        if not content:
            # Belt and braces: if the model still routed everything into the
            # reasoning stream, salvage it rather than dropping the summary.
            content = (msg.get("thinking") or "").strip()
            if content:
                log("  gemma returned empty content; salvaged from thinking stream")
        log(f"  gemma: {len(content)} chars (eval={data.get('eval_count')}, "
            f"done={data.get('done_reason')})")

        # A brief that stops mid-sentence is worse than no brief. Treat a
        # length-truncated or suspiciously short answer as a failed call, retry
        # once on a shorter list, and otherwise fall through to the raw list.
        truncated = data.get("done_reason") == "length" or len(content) < 400
        if truncated and len(items) > 8:
            log("  gemma output truncated; retrying with a shorter list")
            return gemma_summarise(beat_label, items[: len(items) // 2], previous,
                                   instructions, max_items, cloud=False, listing=listing)
        if truncated:
            log("  gemma still truncated; falling back to the raw item list")
            return None
        return content or None
    except Exception as e:
        log(f"  gemma failed: {type(e).__name__}: {e}")
        return None


ANGLES_FLAT_INSTRUCTIONS = (
    "Each headline below is tagged with its outlet. Write a markdown section with:\n"
    "1. **Same story, different angles** — only events GENUINELY covered by both a Western "
    "outlet (BBC, Guardian, DW) and a non-Western outlet; if none, say so in one line. For each, "
    "a ### heading, one line of agreed facts, one bullet per outlet quoting its headline.\n"
    "2. **Only one side is covering** — 2 to 4 bullets naming the outlet.\n\n"
    "Describe framing; do not judge. Never invent coverage."
)
EMBED_MODEL = "nomic-embed-text"
CLUSTER_SIM = 0.84   # average-linkage cosine; tuned 2026-10-08 on 249 headlines
STATE_MEDIA = ("TASS", "CGTN", "Press TV", "Anadolu")


def _embed(texts: list[str]) -> list[list[float]] | None:
    body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    try:
        req = urllib.request.Request(OLLAMA.replace("/api/chat", "/api/embed"), data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            vecs = json.loads(r.read())["embeddings"]
    except Exception as e:
        log(f"  embed failed: {type(e).__name__}: {e}")
        return None
    out = []
    for v in vecs:
        n = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / n for x in v])
    return out


def _is_western(outlet: str) -> bool:
    return outlet.startswith(WESTERN)


def group_angles(items: list[dict]) -> str | None:
    """Group headlines into events in code, so the model compares framing and
    never has to decide what counts as 'the same story'.

    The model left alone missed real matches and invented fake ones (2026-10-08:
    Houthi strike on Riyadh covered by DW, CGTN and Anadolu, reported as
    one-sided; TASS paired with Press TV as 'cross-bloc'). Local nomic embeddings
    + average-linkage clustering found the Houthi, Jerusalem-consulate, Russian
    plague and French protest groups exactly. Returns the listing for the
    prompt, or None if embeddings are unavailable (caller uses the flat list).
    """
    vecs = _embed(["clustering: " + i["title"] + ". "
                   + re.sub(r"<[^>]+>", " ", i.get("summary") or "")[:200] for i in items])
    if not vecs:
        return None
    n = len(items)
    sim = [[sum(a * b for a, b in zip(vecs[i], vecs[j])) for j in range(n)] for i in range(n)]
    groups = [[i] for i in range(n)]
    while True:
        best, ba, bb = CLUSTER_SIM, None, None
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                ga, gb = groups[a], groups[b]
                s = sum(sim[i][j] for i in ga for j in gb) / (len(ga) * len(gb))
                if s >= best:
                    best, ba, bb = s, a, b
        if ba is None:
            break
        groups[ba] += groups.pop(bb)

    cross, single = [], []
    for g in groups:
        outlets = {items[i]["outlet"] for i in g}
        if any(_is_western(o) for o in outlets) and any(not _is_western(o) for o in outlets):
            cross.append(g)
        else:
            single.extend(g)
    # Rank: state media present first, then outlet breadth.
    def rank(g):
        outlets = {items[i]["outlet"] for i in g}
        return (any(o.startswith(STATE_MEDIA) for o in outlets), len(outlets))
    cross.sort(key=rank, reverse=True)
    cross = cross[:8]
    log(f"  angles: {len(groups)} events, {len(cross)} cross-bloc groups kept")

    def line(i):
        it = items[i]
        summ = re.sub(r"<[^>]+>", " ", it.get("summary") or "").strip()[:160]
        return f"- [{it['outlet']}] {it['title']}" + (f"\n  {summ}" if summ else "")

    parts = []
    for k, g in enumerate(cross, 1):
        parts.append(f"GROUP {k}:\n" + "\n".join(line(i) for i in g))
    if not cross:
        parts.append("(no cross-bloc groups today)")
    # Single-bloc candidates: state media first, capped so the prompt stays small.
    single.sort(key=lambda i: not items[i]["outlet"].startswith(STATE_MEDIA))
    parts.append("SINGLE-BLOC (only one bloc carries these):\n"
                 + "\n".join(line(i) for i in single[:30]))
    return "\n\n".join(parts)


WESTERN = ("BBC", "Guardian", "DW")
NON_WESTERN = ("TASS", "CGTN", "Press TV", "Anadolu", "Al Jazeera", "The Hindu", "Dawn", "SCMP")
OFF_TOPIC = re.compile(r"el ni[nñ]o|weather|obituar|dies\b|died\b|earthquake|penguin", re.I)


def enforce_angles(body: str) -> str:
    """Drop comparison blocks the model padded in spite of the prompt.

    Measured 2026-10-08: told "only real cross-bloc matches, never pad", Ling
    still paired TASS with Press TV (both non-Western), DW with BBC (both
    Western), kept a one-outlet block "Hindu does not cover", and El Nino.
    Prompts do not hold this rule, so code does: a ### block under "Same story"
    survives only if its bullets name at least one Western AND one non-Western
    outlet, no bullet says the outlet does not cover it, and it is not weather/
    obituary. Bullets that admit non-coverage are removed first.
    """
    head, sep, rest = body.partition("## Only one side")
    blocks = re.split(r"(?m)^(?=### )", head)
    kept, dropped = [blocks[0]], []
    for b in blocks[1:]:
        lines = [l for l in b.splitlines()
                 if not re.search(r"does not (directly )?(cover|report)|not cover(ed)?|excluded from", l, re.I)]
        bullets = "\n".join(l for l in lines if re.match(r"\s*[-*]\s", l))
        has_w = any(o in bullets for o in WESTERN)
        has_n = any(o in bullets for o in NON_WESTERN)
        title = lines[0] if lines else ""
        if has_w and has_n and not OFF_TOPIC.search(title):
            kept.append("\n".join(lines) + "\n\n")
        else:
            dropped.append(title.lstrip("# ").strip())
    if dropped:
        log(f"  angles: dropped {len(dropped)} padded comparison(s): {dropped}")
    if len(kept) == 1:
        kept.append("No story today was covered by both a Western and a non-Western outlet in these feeds.\n\n")
    return "".join(kept).rstrip() + "\n\n" + (sep + rest if sep else "")


def raw_fallback(beat_label: str, items: list[dict]) -> str:
    lines = [f"### {beat_label} — raw items (gemma unavailable)", ""]
    for i in items[:25]:
        lines.append(f"- **{i['title']}**" + (f" — {i['url']}" if i["url"] else ""))
    return "\n".join(lines)


def run_beat(beat: str, cfg: dict, use_llm: bool, dry_run: bool) -> tuple[str, list[str]]:
    today = dt.date.today().isoformat()
    label = cfg["label"]
    WRITER["name"] = ""
    problems: list[str] = []
    items: list[dict] = []

    # gemma only sees the first 30 items, so feeds are interleaved round-robin;
    # appended in order, the last feeds in the list would never reach the model.
    per_feed: list[list[dict]] = []
    for entry in cfg["rss"]:
        outlet, feed = entry if isinstance(entry, tuple) else ("", entry)
        got, err = fetch_rss(feed, limit=cfg.get("per_feed", 12), outlet=outlet)
        if err:
            problems.append(err)
        per_feed.append(got)
        log(f"  rss {feed.split('/')[2] if '//' in feed else feed}: {len(got)} items")

    sweep_out = os.path.join(RESEARCH_DIR, f".sweep-{beat}-{today}.md")
    swept, err = fetch_sweep(beat, cfg["sweep"], sweep_out) if cfg["sweep"] else ([], None)
    if err:
        problems.append(err)
    per_feed.append(swept)
    for row in range(max((len(f) for f in per_feed), default=0)):
        items.extend(f[row] for f in per_feed if row < len(f))
    log(f"  sweep: {len(swept)} items")
    if os.path.exists(sweep_out):
        os.remove(sweep_out)

    # de-dup on title
    seen, uniq = set(), []
    for i in items:
        k = re.sub(r"\W+", "", i["title"].lower())[:80]
        if k and k not in seen:
            seen.add(k)
            uniq.append(i)
    items = uniq
    log(f"  {len(items)} unique items")

    # Drop anything an earlier brief already sent (by URL or normalised title).
    backlog = load_backlog(beat, dt.date.fromisoformat(today))
    fresh = [i for i in items
             if i["url"] not in backlog["urls"] and _title_key(i["title"]) not in backlog["keys"]]
    repeats = len(items) - len(fresh)
    log(f"  {len(fresh)} new vs backlog ({repeats} already sent in last {BACKLOG_DAYS}d, "
        f"{len(backlog['briefs'])} prior briefs as context)")
    items = fresh

    if not items:
        body = "## Nothing new\n\nEvery item in today's feeds was already covered in an earlier brief."
    else:
        angles = cfg.get("mode") == "angles"
        grouped = group_angles(items) if (angles and use_llm) else None
        if angles and grouped is None:
            problems.append("embeddings unavailable; angles written from the flat list")
        body = gemma_summarise(
            label, items, backlog["briefs"],
            instructions=ANGLES_INSTRUCTIONS if grouped else (
                ANGLES_FLAT_INSTRUCTIONS if angles else None),
            max_items=66 if angles else 30,   # flat fallback: ~6 headlines per outlet
            listing=grouped,
        ) if use_llm else None
    if not body:
        body = raw_fallback(label, items)
    elif items:
        # The model likes to emit its own top-level heading. Demote it so the beat
        # title is the only h1 in the rendered email, instead of two stacked.
        body = re.sub(r"^# ", "## ", body, flags=re.M)
        body = re.sub(r"(?m)^#+\s*$\n?", "", body)   # stray bare '#' lines
        if cfg.get("mode") == "angles":
            body = enforce_angles(body)

    report = "\n".join([
        f"# {label} — {today}",
        "",
        f"*Generated {dt.datetime.now():%Y-%m-%d %H:%M} by research-daily.py "
        f"on {WRITER['name'] or 'raw mode'}.*",
        "",
        f"Sources: {len(items)} new items ({repeats} already sent, skipped). "
        + (f"Problems: {'; '.join(problems)}" if problems else "All sources OK."),
        "",
        body,
        "",
        "---",
        "",
        "## Sources",
        *[f"- [{i['title']}]({i['url']})" for i in items[:40] if i["url"]],
        "",
    ])

    # Dry runs write beside the archive, never into it: the archive is the
    # backlog tomorrow's run dedupes against, so a dry run must not overwrite it.
    out_dir = os.path.join(RESEARCH_DIR, ".dry-run") if dry_run else RESEARCH_DIR
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{beat}-{today}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
    log(f"  wrote {out} ({len(report)} chars)")
    return report, problems


def reindex() -> str:
    env = {
        **os.environ,
        "RESEARCH_DIR": os.environ.get("RD_INDEX_ROOT", HOME),
    }
    if not os.path.exists(INDEX):
        return "no indexer configured (RD_INDEX); skipped"
    try:
        r = subprocess.run(
            [sys.executable, INDEX], capture_output=True, text=True, timeout=120, env=env
        )
        return (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else "no output"
    except Exception as e:
        return f"index failed: {type(e).__name__}: {e}"


def _esc(s: str) -> str:
    return html_module.escape(s, quote=False)


def _md_inline(s: str) -> str:
    s = _esc(s)
    s = re.sub(r"\[(.+?)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"`([^`]+?)`", r"<code>\1</code>", s)
    return s


def split_sources(md: str) -> tuple[str, list[str]]:
    """Pull the trailing '## Sources' block out of a report.

    The per-beat archive keeps its own source list for provenance, but the email
    consolidates them into one list at the foot.
    """
    marker = "\n## Sources"
    if marker not in md:
        return md.rstrip(), []
    body, _, tail = md.rpartition(marker)
    links = re.findall(r"^\s*[-*]\s+\[.*$", tail, flags=re.M)
    return body.rstrip(), links


def md_to_html(md: str) -> str:
    """Markdown subset to HTML. Handles what the briefs actually emit."""
    out: list[str] = []
    list_open = False
    src_open = False

    def close_list():
        nonlocal list_open
        if list_open:
            out.append("</ul>")
            list_open = False

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            close_list()
            continue
        if re.match(r"^\s*[-*]\s+", line):
            if not list_open:
                out.append('<ul>')
                list_open = True
            item = re.sub(r"^\s*[-*]\s+", "", line)
            out.append(f"<li>{_md_inline(item)}</li>")
            continue
        close_list()
        if line.strip() == "---":
            out.append("<hr>")
        elif line.startswith("### "):
            out.append(f"<h3>{_md_inline(line[4:])}</h3>")
        elif line.startswith("## "):
            title = line[3:].strip()
            if title.lower().startswith("sources") and not src_open:
                out.append('<div class="sources">')
                src_open = True
            out.append(f"<h2>{_md_inline(title)}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{_md_inline(line[2:])}</h1>")
        elif line.startswith("> "):
            out.append(f"<blockquote>{_md_inline(line[2:])}</blockquote>")
        elif line.startswith("*") and line.endswith("*") and len(line) > 2:
            out.append(f"<p class='muted'><em>{_md_inline(line.strip('*'))}</em></p>")
        else:
            out.append(f"<p>{_md_inline(line)}</p>")
    close_list()
    if src_open:
        out.append("</div>")
    return "\n".join(out)


def render_html(subject: str, md: str) -> str:
    """Render the digest as a self-contained HTML email (inline styles only)."""
    body = md_to_html(md)
    css = """
    body{margin:0;padding:0;background:#eef1f5;}
    .wrap{width:100%;background:#eef1f5;padding:28px 12px;}
    .card{max-width:680px;margin:0 auto;background:#ffffff;border-radius:10px;
          overflow:hidden;border:1px solid #e2e6ec;}
    .head{background:#111827;padding:26px 32px;}
    .head h1{margin:0;color:#ffffff;font-size:21px;line-height:1.3;font-weight:650;
             letter-spacing:-0.2px;}
    .head .sub{margin:8px 0 0;color:#9aa4b2;font-size:13px;line-height:1.5;}
    .body{padding:28px 32px 34px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',
          Roboto,Helvetica,Arial,sans-serif;color:#1f2937;font-size:15.5px;line-height:1.65;}
    h1{font-size:19px;line-height:1.35;margin:30px 0 10px;color:#111827;font-weight:650;
       letter-spacing:-0.2px;padding-top:22px;border-top:1px solid #e8ecf1;}
    .body h1:first-child{border-top:none;padding-top:0;margin-top:4px;}
    h2{font-size:16px;margin:26px 0 8px;color:#111827;font-weight:650;}
    h3{font-size:15.5px;margin:22px 0 8px;color:#1e40af;font-weight:620;line-height:1.5;}
    p{margin:0 0 13px;}
    ul{margin:0 0 15px;padding-left:20px;}
    li{margin:0 0 9px;}
    strong{color:#111827;font-weight:640;}
    em{color:#374151;}
    a{color:#1d4ed8;text-decoration:none;border-bottom:1px solid #c7d5f0;}
    a:hover{text-decoration:underline;}
    code{background:#f1f4f8;padding:1px 5px;border-radius:4px;font-size:13px;
         font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
    blockquote{margin:0 0 16px;padding:11px 16px;background:#f7f9fc;
               border-left:3px solid #2563eb;border-radius:0 6px 6px 0;color:#374151;}
    hr{border:none;border-top:1px solid #e8ecf1;margin:28px 0;}
    .muted{color:#6b7280;}
    .sources{margin-top:10px;}
    .sources h2{font-size:12px;text-transform:uppercase;letter-spacing:0.8px;
                color:#6b7280;margin:0 0 12px;font-weight:650;}
    .sources h3{font-size:12.5px;color:#374151;font-weight:650;margin:15px 0 6px;}
    .sources ul{margin:0 0 12px;padding-left:18px;}
    .sources li{margin:0 0 5px;font-size:12.5px;line-height:1.55;color:#4b5563;}
    .sources a{color:#4b5563;border-bottom:none;}
    .foot{padding:20px 32px 26px;background:#f7f9fc;border-top:1px solid #e8ecf1;
          color:#6b7280;font-size:12.5px;line-height:1.6;
          font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;}
    .foot a{color:#4b5563;border-bottom:none;}
    """
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(subject)}</title>
<style>{css}</style></head>
<body><div class="wrap"><div class="card">
<div class="head">
  <h1>{_esc(subject.split(' — ')[0] if ' — ' in subject else subject)}</h1>
  <p class="sub">{_esc(subject.split(' — ', 1)[1] if ' — ' in subject else '')}<br>
  Generated {_esc(dt.date.today().isoformat())} by research-daily.py ·
  Ling 3.0 Flash (OpenRouter), gemma4:26b fallback</p>
</div>
<div class="body">{body}</div>
<div class="foot">Archived as markdown and indexed into the local full-text corpus. Sources are linked inline and listed in full
at the end of each section.<br>This brief is generated from public feeds. Verify before
you rely on it.</div>
</div></div></body></html>"""


def send_email(subject: str, body: str) -> tuple[bool, str]:
    """multipart/alternative: text/plain + text/html.

    text/markdown was being rendered as an opaque blob or a stray attachment by
    most clients. HTML is the only thing that reliably formats in an inbox.
    """
    msg = EmailMessage()
    msg["To"] = MAIL_TO
    msg["From"] = MAIL_FROM
    msg["Subject"] = subject
    # Plain part first: the fallback every client can read.
    plain = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1 (\2)", body)
    plain = re.sub(r"^[#>*]+\s?", "", plain, flags=re.M)
    msg.set_content(plain)
    msg.add_alternative(render_html(subject, body), subtype="html")

    payload = msg.as_bytes()
    for cmd in ([HIMALAYA, "message", "send"], [HIMALAYA, "send"], ["himalaya", "message", "send"]):
        try:
            r = subprocess.run(cmd, input=payload, capture_output=True, timeout=120)
            if r.returncode == 0:
                return True, " ".join(cmd)
            last = ((r.stderr or r.stdout or b"").decode(errors="replace")).strip().splitlines()[-1:]
        except FileNotFoundError:
            last = ["himalaya not on PATH"]
        except Exception as e:
            last = [f"{type(e).__name__}: {e}"]
    return False, "; ".join(last) if last else "unknown send failure"


def main() -> int:
    global MAIL_TO
    ap = argparse.ArgumentParser(description="daily research newsletter (AI, geopolitics, markets)")
    ap.add_argument("--beats", nargs="+", choices=list(BEATS), default=list(BEATS))
    ap.add_argument("--dry-run", action="store_true", help="write reports, do not email")
    ap.add_argument("--no-llm", action="store_true", help="skip gemma, ship raw item lists")
    ap.add_argument("--to", default=MAIL_TO)
    args = ap.parse_args()

    MAIL_TO = args.to
    if not args.dry_run and not MAIL_TO:
        ap.error("no recipient: set RD_MAIL_TO or pass --to")
    os.makedirs(RESEARCH_DIR, exist_ok=True)

    use_llm = not args.no_llm
    today = dt.date.today().isoformat()
    reports: dict[str, str] = {}
    all_problems: list[str] = []

    for beat in args.beats:
        log(f"beat: {beat}")
        rep, problems = run_beat(beat, BEATS[beat], use_llm, args.dry_run)
        reports[beat] = rep
        all_problems.extend(f"{beat}: {p}" for p in problems)

    if args.dry_run:
        log("dry-run: archive and index untouched")
    else:
        log("reindexing corpus")
        log(f"  {reindex()}")

    subject = f"Research brief — {today} — " + ", ".join(BEATS[b]["label"] for b in args.beats)

    # Sources belong in one consolidated list at the foot of the email, not
    # repeated after every beat. Keep them in the per-beat .md archive on disk,
    # but pull them out here for the reading surface.
    bodies: list[str] = []
    source_blocks: list[str] = []
    for beat in args.beats:
        body, links = split_sources(reports[beat])
        bodies.append(body)
        if links:
            source_blocks.append(f"### {BEATS[beat]['label']}\n\n" + "\n".join(links))
    digest = "\n\n".join(bodies)
    if all_problems:
        digest += "\n\n---\n\n### Source problems\n\n" + "\n".join(f"- {p}" for p in all_problems)
    if source_blocks:
        digest += "\n\n---\n\n## Sources\n\n" + "\n\n".join(source_blocks)

    if args.dry_run:
        log("dry-run: not sending")
        print("\n" + "=" * 70 + "\n")
        print(f"Subject: {subject}\n")
        print(digest[:4000])
        return 0

    ok, detail = send_email(subject, digest)
    log(f"email: {'SENT via ' + detail if ok else 'FAILED: ' + detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
