"""Bounded public-web collection for local-first project research."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx


MAX_SEARCH_HTML_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_SOURCE_CHARS = 40_000
MAX_REDIRECTS = 3


class PublicWebResearchError(RuntimeError):
    pass


def _decode_html_response(response: httpx.Response) -> str:
    """Decode Japanese public pages without trusting an incorrect HTTP default."""
    data = response.content
    candidates: list[str] = []
    content_type = response.headers.get("content-type", "")
    header_match = re.search(r"charset\s*=\s*[\"'']?([^;\s\"'']+)", content_type, re.I)
    if header_match:
        candidates.append(header_match.group(1))
    meta_match = re.search(
        br"charset\s*=\s*[\"'']?\s*([a-zA-Z0-9._-]+)", data[:8192], re.I,
    )
    if meta_match:
        candidates.append(meta_match.group(1).decode("ascii", errors="ignore"))
    candidates.extend(("utf-8-sig", "utf-8", "cp932", "shift_jis", "euc_jp"))
    seen: set[str] = set()
    for encoding in candidates:
        normalized = encoding.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            return data.decode(normalized, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def official_domain_hints(query: str) -> tuple[str, ...]:
    text = query.lower()
    if re.search(r"ものづくり|モノづくり|補助金|公募要領|中小企業庁", text):
        return ("chusho.meti.go.jp", "monodukuri-hojo.jp", "jgrants-portal.go.jp")
    if re.search(r"道路運送|運送業|点呼|国土交通省", text):
        return ("mlit.go.jp",)
    if re.search(r"労働|年休|厚生労働省", text):
        return ("mhlw.go.jp",)
    return ()


def _global_address(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


async def validate_public_https_url(url: str, resolver=socket.getaddrinfo) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise PublicWebResearchError("公開Web取得は認証情報を含まないHTTPS URLだけを許可します")
    if parsed.port not in {None, 443}:
        raise PublicWebResearchError("公開Web取得はHTTPS標準ポートだけを許可します")
    try:
        records = await asyncio.to_thread(resolver, parsed.hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PublicWebResearchError(f"Web取得先を名前解決できません: {parsed.hostname}") from exc
    addresses = {str(record[4][0]).split("%", 1)[0] for record in records if record[4]}
    if not addresses or any(not _global_address(address) for address in addresses):
        raise PublicWebResearchError("ローカル・プライベート・予約済みアドレスへのWeb取得を拒否しました")
    return parsed.geturl()


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._href = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        classes = set(str(values.get("class", "")).split())
        if tag == "a" and classes.intersection({"result__a", "result-link"}):
            self._href = str(values.get("href", ""))
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href and data.strip():
            self._parts.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.results.append({"url": self._href, "title": " ".join(self._parts).strip()})
            self._href = ""
            self._parts = []


class _ArticleParser(HTMLParser):
    BLOCKS = {"p", "div", "article", "section", "main", "li", "h1", "h2", "h3", "h4", "tr", "br"}
    SKIP = {"script", "style", "noscript", "svg", "canvas", "nav", "footer"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if not self._skip_depth and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        if not self._skip_depth and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data.strip():
            return
        value = re.sub(r"\s+", " ", data).strip()
        self.parts.append(value)
        if self._in_title:
            self.title_parts.append(value)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", " ".join(self.parts).replace(" \n ", "\n")).strip()


def _result_url(href: str) -> str:
    target = urljoin("https://html.duckduckgo.com/", href)
    parsed = urlsplit(target)
    if parsed.hostname and (
        parsed.hostname == "duckduckgo.com"
        or parsed.hostname.endswith(".duckduckgo.com")
    ):
        redirected = parse_qs(parsed.query).get("uddg", [])
        if redirected:
            target = unquote(redirected[0])
    return target


def render_web_source(source: dict[str, str]) -> str:
    return (
        "# 公開Web取得資料\n\n"
        f"- URL: {source['url']}\n"
        f"- タイトル: {source.get('title') or '取得ページ'}\n"
        f"- 取得日時(UTC): {source['retrieved_at']}\n"
        f"- 公的候補: {'はい' if source.get('official') else 'いいえ'}\n"
        f"- 本文SHA256: {source['sha256']}\n\n"
        "## 抽出本文\n\n"
        + source["content"].strip()
        + "\n"
    )


class PublicWebResearcher:
    def __init__(self, transport=None, resolver=socket.getaddrinfo) -> None:
        self.transport = transport
        self.resolver = resolver
        self.search_url = os.getenv("PUBLIC_WEB_SEARCH_URL", "https://html.duckduckgo.com/html/")

    async def _get(self, client: httpx.AsyncClient, url: str, *, params=None) -> httpx.Response:
        current = url
        current_params = params
        for _ in range(MAX_REDIRECTS + 1):
            await validate_public_https_url(current, self.resolver)
            response = await client.get(current, params=current_params)
            current_params = None
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location", "")
            if not location:
                return response
            current = urljoin(str(response.url), location)
        raise PublicWebResearchError("Web取得のリダイレクト回数が上限を超えました")

    async def collect(self, query: str, max_results: int = 4) -> dict:
        query = re.sub(r"\s+", " ", query).strip()
        if not query or len(query) > 500:
            raise PublicWebResearchError("Web検索語は1～500文字で指定してください")
        max_results = max(1, min(int(max_results), 6))
        hints = official_domain_hints(query)
        timeout = httpx.Timeout(float(os.getenv("PUBLIC_WEB_TIMEOUT", "30")), connect=10)
        headers = {"User-Agent": "LocalCowork-PublicResearch/1.0 (+local evidence collector)"}
        async with httpx.AsyncClient(
            timeout=timeout, headers=headers, follow_redirects=False,
            transport=self.transport, trust_env=False,
        ) as client:
            search = await self._get(client, self.search_url, params={"q": query})
            if search.status_code >= 400:
                raise PublicWebResearchError(f"Web検索に失敗しました: HTTP {search.status_code}")
            if len(search.content) > MAX_SEARCH_HTML_BYTES:
                raise PublicWebResearchError("Web検索結果がサイズ上限を超えました")
            parser = _SearchParser()
            parser.feed(_decode_html_response(search))
            candidates = []
            seen = set()
            for order, item in enumerate(parser.results):
                url = _result_url(item["url"])
                if url in seen or urlsplit(url).scheme.lower() != "https":
                    continue
                seen.add(url)
                host = (urlsplit(url).hostname or "").lower()
                official = any(host == domain or host.endswith("." + domain) for domain in hints)
                candidates.append({**item, "url": url, "official": official, "order": order})
            candidates.sort(key=lambda item: (not item["official"], item["order"]))
            sources = []
            errors = []
            for item in candidates[: max_results * 3]:
                if len(sources) >= max_results:
                    break
                try:
                    response = await self._get(client, item["url"])
                    if response.status_code >= 400:
                        raise PublicWebResearchError(f"HTTP {response.status_code}")
                    if len(response.content) > MAX_SOURCE_BYTES:
                        raise PublicWebResearchError("ページが5MB上限を超えています")
                    media = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if media == "application/pdf" or urlsplit(str(response.url)).path.lower().endswith(".pdf"):
                        from app.context_files import extract_context_file

                        extracted = extract_context_file("source.pdf", response.content, "application/pdf")
                        content = extracted.content
                        title = item["title"] or urlsplit(str(response.url)).path.rsplit("/", 1)[-1]
                    elif media.startswith("text/") or media in {"application/xhtml+xml", ""}:
                        article = _ArticleParser()
                        article.feed(_decode_html_response(response))
                        content = article.text()
                        title = " ".join(article.title_parts).strip() or item["title"]
                    else:
                        raise PublicWebResearchError(f"未対応Content-Type: {media or 'unknown'}")
                    content = content[:MAX_SOURCE_CHARS].strip()
                    if len(content) < 120:
                        raise PublicWebResearchError("抽出本文が短すぎます")
                    source_url = str(response.url)
                    host = (urlsplit(source_url).hostname or "").lower()
                    sources.append({
                        "url": source_url,
                        "title": title[:300],
                        "content": content,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        "official": any(host == domain or host.endswith("." + domain) for domain in hints),
                    })
                except (httpx.HTTPError, PublicWebResearchError, UnicodeError) as exc:
                    errors.append({"url": item["url"], "error": str(exc)[:500]})
            if not sources:
                raise PublicWebResearchError("安全に取得できる公開Web資料が見つかりませんでした")
            return {"query": query, "official_domains": list(hints), "sources": sources, "errors": errors}
