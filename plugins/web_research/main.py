"""web_research — the first SHAFT plugin, and a worked example for writing your own.

A plugin is a folder plugins/<name>/ with plugin.json (name, description, JSON-schema
parameters) and main.py defining run(**params) -> str. It runs in its own process
inside my sandbox; the supervisor's tools are reachable through `shaft.broker`.
"""
from __future__ import annotations

import re

from shaft import broker

PER_PAGE_CHARS = 2500


def _urls(search_results: str) -> list[str]:
    return re.findall(r"^\s+(https?://\S+)$", search_results, re.M)


def run(question: str, pages: int = 3) -> str:
    pages = max(1, min(int(pages), 5))
    results = broker.call("web_search", query=question)
    urls = _urls(results)[:pages]
    if not urls:
        return f"No results for {question!r}.\n\n{results}"
    sections = []
    for i, url in enumerate(urls, 1):
        try:
            text = broker.call("fetch_page", url=url, max_chars=PER_PAGE_CHARS)
        except broker.BrokerError as e:
            text = f"(could not read: {e})"
        sections.append(f"## Source {i}: {url}\n{text}")
    return (f"# Research: {question}\n\n## Search results\n{results}\n\n" + "\n\n".join(sections)
            + "\n\n(Pages were read by the supervisor's broker; quote sources by URL.)")
