#!/usr/bin/env python3
"""Fetch RSS feeds, summarize them with an LLM, and produce stock watch notes."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


LOG = logging.getLogger("ai-stock-monitor")
TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    slug: str


class MaximumLevelFilter(logging.Filter):
    """Allow records up to a level, for separating stdout and stderr logs."""

    def __init__(self, maximum_level: int) -> None:
        super().__init__()
        self.maximum_level = maximum_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.maximum_level


def configure_logging(verbose: bool) -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    output_handler = logging.StreamHandler(sys.stdout)
    output_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    output_handler.addFilter(MaximumLevelFilter(logging.INFO))
    output_handler.setFormatter(formatter)

    error_handler = logging.StreamHandler(sys.stderr)
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(output_handler)
    root_logger.addHandler(error_handler)


def slugify(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.").lower()
    if not result:
        raise ValueError(f"Cannot create a directory name from {value!r}")
    return result


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot read config {path}: {exc}") from exc

    required = ("rss_channels", "stocks", "llm")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError(f"Missing required configuration: {', '.join(missing)}")
    if not isinstance(config["rss_channels"], list) or not isinstance(config["stocks"], list):
        raise ValueError("rss_channels and stocks must be YAML lists")
    return config


def configured_feeds(config: dict[str, Any]) -> list[Feed]:
    feeds: list[Feed] = []
    for item in config["rss_channels"]:
        if not isinstance(item, dict) or not item.get("name") or not item.get("url"):
            raise ValueError("Each rss_channels item needs name and url")
        feeds.append(Feed(str(item["name"]), str(item["url"]), slugify(str(item.get("slug") or item["name"]))))
    return feeds


def fetch(url: str, timeout: int, user_agent: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child_text(element: ET.Element, names: tuple[str, ...]) -> str:
    for child in element:
        if local_name(child.tag) in names:
            return "".join(child.itertext()).strip()
    return ""


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", value))).strip()


def parse_feed(raw: bytes, max_items: int) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"Invalid RSS/Atom XML: {exc}") from exc

    entries = [node for node in root.iter() if local_name(node.tag) in ("item", "entry")]
    articles: list[dict[str, str]] = []
    for entry in entries[:max_items]:
        link = child_text(entry, ("link",))
        if not link:
            for child in entry:
                if local_name(child.tag) == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        articles.append(
            {
                "title": clean_text(child_text(entry, ("title",))),
                "published": clean_text(child_text(entry, ("pubdate", "published", "updated", "date"))),
                "link": link,
                "description": clean_text(child_text(entry, ("description", "summary", "content", "encoded"))),
            }
        )
    return articles


def articles_as_text(feed: Feed, articles: list[dict[str, str]], max_chars: int) -> str:
    parts = [f"SOURCE: {feed.name}"]
    for number, article in enumerate(articles, 1):
        parts.append(
            f"\n[{number}] {article['title']}\n"
            f"Published: {article['published'] or 'unknown'}\n"
            f"URL: {article['link']}\n"
            f"Content: {article['description']}"
        )
    return "\n".join(parts)[:max_chars]


def call_llm(config: dict[str, Any], system_prompt: str, user_prompt: str) -> str:
    llm = config["llm"]
    api_key_env = str(llm.get("api_key_env", "OPENAI_API_KEY"))
    api_key = os.environ.get(api_key_env, "")

    base_url = str(llm["base_url"]).rstrip("/")
    endpoint = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
    payload = {
        "model": llm["model_name"],
        "temperature": float(llm.get("temperature", 0.2)),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=int(llm.get("timeout_seconds", 120))) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError) as exc:
        raise RuntimeError(f"LLM request failed: {exc}") from exc


def history_text(data_dir: Path, feeds: list[Feed], days: int, today: date, max_chars: int) -> str:
    """Combine recent summaries from successful feeds and prior combined advice."""
    earliest = today - timedelta(days=max(days - 1, 0))
    records: list[tuple[date, str, Path]] = []
    for feed in feeds:
        # Read the current date-first layout plus the former feed-first layout,
        # so changing storage structure does not discard historical context.
        candidates = list(data_dir.glob(f"*/{feed.slug}/summary.txt"))
        candidates.extend((data_dir / feed.slug).glob("*/summary.txt"))
        seen_paths: set[Path] = set()
        for path in candidates:
            if path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                if path.parent.name == feed.slug:
                    file_date = date.fromisoformat(path.parent.parent.name)
                else:
                    file_date = date.fromisoformat(path.parent.name)
            except ValueError:
                continue
            if earliest <= file_date <= today:
                records.append((file_date, feed.name, path))
    records.sort()
    chunks = [
        f"## NEWS SUMMARY — {file_date.isoformat()} — {feed_name}\n"
        f"{path.read_text(encoding='utf-8')}"
        for file_date, feed_name, path in records
    ]

    # Include earlier combined recommendations, but never today's output. This
    # prevents --force from recursively treating an old same-day answer as input.
    previous_advice: list[tuple[date, Path]] = []
    advice_candidates = list(data_dir.glob("*/advice.txt"))
    # Former centralized layout, retained as read-only historical input.
    advice_candidates.extend((data_dir / "advices").glob("*.txt"))
    seen_advice_paths: set[Path] = set()
    for path in advice_candidates:
        if path in seen_advice_paths:
            continue
        seen_advice_paths.add(path)
        try:
            advice_date = date.fromisoformat(
                path.parent.name if path.name == "advice.txt" else path.stem
            )
        except ValueError:
            continue
        if earliest <= advice_date < today:
            previous_advice.append((advice_date, path))
    for advice_date, path in sorted(previous_advice):
        chunks.append(
            f"## PREVIOUS COMBINED ADVICE — {advice_date.isoformat()}\n"
            f"{path.read_text(encoding='utf-8')}"
        )
    text = "\n\n".join(chunks)
    # Preserve the most recent material if the context limit is reached.
    return text[-max_chars:]


def atomic_write(path: Path, content: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if isinstance(content, str):
        temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
    else:
        temporary.write_bytes(content)
    temporary.replace(path)


def run(config: dict[str, Any], run_date: date, force: bool) -> int:
    data_dir = Path(config.get("data_dir", "data")).expanduser()
    if not data_dir.is_absolute():
        data_dir = Path.cwd() / data_dir
    fetch_config = config.get("fetch", {})
    max_items = int(fetch_config.get("max_items_per_feed", 30))
    max_chars = int(config["llm"].get("max_input_chars", 50000))
    timeout = int(fetch_config.get("timeout_seconds", 30))
    user_agent = str(fetch_config.get("user_agent", "ai-stock-monitor/1.0"))
    summary_prompt = str(config["llm"].get("summary_prompt", DEFAULT_SUMMARY_PROMPT))

    successful_feeds: list[Feed] = []
    feeds = configured_feeds(config)
    LOG.info("Starting daily run for %s (%d RSS channels)", run_date.isoformat(), len(feeds))
    for feed_number, feed in enumerate(feeds, 1):
        day_dir = data_dir / run_date.isoformat() / feed.slug
        xml_path = day_dir / "rss.xml"
        summary_path = day_dir / "summary.txt"
        try:
            LOG.info("[%d/%d] Reading RSS channel: %s", feed_number, len(feeds), feed.name)
            raw = fetch(feed.url, timeout, user_agent)
            atomic_write(xml_path, raw)
            LOG.info("[%d/%d] Parsing RSS channel: %s", feed_number, len(feeds), feed.name)
            articles = parse_feed(raw, max_items)
            if not articles:
                raise RuntimeError("Feed contains no RSS items or Atom entries")
            if force or not summary_path.exists():
                LOG.info(
                    "[%d/%d] Summarizing today's news: %s (%d articles)",
                    feed_number,
                    len(feeds),
                    feed.name,
                    len(articles),
                )
                summary = call_llm(config, summary_prompt, articles_as_text(feed, articles, max_chars))
                atomic_write(summary_path, summary)
            else:
                LOG.info("[%d/%d] Summary already exists; skipping: %s", feed_number, len(feeds), feed.name)
            LOG.info("[%d/%d] Finished RSS channel: %s", feed_number, len(feeds), feed.name)
            successful_feeds.append(feed)
        except Exception as exc:  # continue processing independent feeds
            LOG.error("Could not process %s: %s", feed.name, exc)

    if not successful_feeds:
        return 1

    history_days = int(config.get("advice", {}).get("history_days", 30))
    advice_language = str(config.get("advice", {}).get("language", "en")).strip() or "en"
    advice_path = data_dir / run_date.isoformat() / "advice.txt"
    try:
        if force or not advice_path.exists():
            feed_names = ", ".join(feed.name for feed in successful_feeds)
            LOG.info(
                "Reading %d days of summaries from %d successful channels: %s",
                history_days,
                len(successful_feeds),
                feed_names,
            )
            history = history_text(data_dir, successful_feeds, history_days, run_date, max_chars)
            if not history:
                raise RuntimeError("No summaries available for advice")
            stock_list = "\n".join(f"- {stock}" for stock in config["stocks"])
            advice_prompt = str(config.get("advice", {}).get("prompt", DEFAULT_ADVICE_PROMPT))
            user_prompt = (
                f"Successful RSS channels: {feed_names}\n"
                f"Analysis date: {run_date.isoformat()}\n"
                f"Lookback: {history_days} days\n"
                f"Required response language: {advice_language}\n"
                f"Write the entire advice response in {advice_language}. Preserve proper nouns, "
                f"ticker symbols, URLs, and necessary source quotations in their original form.\n"
                f"Stocks of interest:\n{stock_list}\n\n"
                f"DATED NEWS SUMMARIES FROM ALL SUCCESSFUL CHANNELS AND PREVIOUS ADVICE:\n{history}"
            )
            LOG.info(
                "Generating one combined advice for %d stocks from %d successful channels",
                len(config["stocks"]),
                len(successful_feeds),
            )
            atomic_write(advice_path, call_llm(config, advice_prompt, user_prompt))
        else:
            LOG.info("Today's combined advice already exists; skipping")
        LOG.info("Combined advice ready: %s", advice_path)
    except Exception as exc:
        LOG.error("Could not produce combined advice: %s", exc)
        return 1
    LOG.info("Daily run complete")
    return 0


DEFAULT_SUMMARY_PROMPT = """You are a careful financial-news editor. Summarize only the supplied feed items.
Prioritize market-moving facts, company events, earnings, guidance, regulation, macroeconomic developments, and risks.
Use concise Markdown with: (1) Key developments, (2) Companies/tickers mentioned, (3) Potential market impact.
Include article URLs beside relevant claims. Distinguish reported facts from opinions, do not invent missing details,
and explicitly note conflicting or uncertain information."""

DEFAULT_ADVICE_PROMPT = """You are a cautious equity research assistant. Analyze only the supplied dated news summaries
from all successful channels. First assess overall market sentiment as bullish, neutral, bearish, or mixed; give its intensity
(1-5) and confidence (low/medium/high). Explain the main macro, sector, risk-appetite, and event drivers with dated sources.

For every requested stock provide: relevant developments; news sentiment (bullish/neutral/bearish/mixed); a sentiment score
from -2 (strongly bearish) to +2 (strongly bullish); confidence; bullish and bearish evidence; near-term catalysts and risks;
important unknowns; and a watch/positive/negative stance. Identify changes versus previous advice when available. Reconcile
conflicting reports explicitly, avoid double-counting the same story across channels, and distinguish broad market sentiment
from company-specific sentiment. Do not infer investor positioning or price action unless the supplied news states it. Never
fabricate prices, fundamentals, events, or consensus. This is news-based research, not personalized financial advice; finish
with a brief risk disclaimer. Follow the response language specified in the user message."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yml"), help="YAML configuration file")
    parser.add_argument("--date", help="Run date in YYYY-MM-DD (defaults to local date)")
    parser.add_argument("--force", action="store_true", help="Regenerate existing summaries and advice")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    configure_logging(args.verbose)
    try:
        run_date = date.fromisoformat(args.date) if args.date else datetime.now().astimezone().date()
        return run(load_config(args.config), run_date, args.force)
    except (ValueError, KeyError) as exc:
        LOG.error("Configuration error: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
