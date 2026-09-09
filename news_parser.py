"""Bounded, network-free extraction helpers shared by feed and article parsers."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


_BLOCKS = frozenset({
    'address', 'article', 'aside', 'blockquote', 'br', 'dd', 'div', 'dl', 'dt',
    'figcaption', 'figure', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'header', 'hr', 'li', 'main', 'ol', 'p', 'pre', 'section', 'table', 'td',
    'th', 'tr', 'ul',
})
_HIDDEN = frozenset({'script', 'style', 'noscript', 'template', 'svg'})


class _FragmentText(HTMLParser):
    """Keep inline typography, separate block boundaries, omit executable text."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _HIDDEN:
            if not self.hidden:
                self.parts.append(' ')
            self.hidden.append(tag)
        elif not self.hidden and tag in _BLOCKS:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                index = len(self.hidden) - 1 - self.hidden[::-1].index(tag)
                del self.hidden[index:]
            return
        if tag in _BLOCKS:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def handle_entityref(self, name):
        self.handle_data('&' + name + ';')

    def handle_charref(self, name):
        self.handle_data('&#' + name + ';')


def clean_html_fragment(value: str) -> str:
    """HTML to plain text without merging paragraphs or splitting inline words."""
    if not value:
        return ''
    parser = _FragmentText()
    parser.feed(value)
    parser.close()
    text = html.unescape(''.join(parser.parts))
    return re.sub(r'\s+', ' ', text.replace('\u200b', '')).strip()


_JUNK_SELECTORS = (
    'script, style, nav, header, footer, aside, form, noscript, iframe, '
    'figure, template, svg, [hidden], [aria-hidden="true"], '
    '.share, .social, .related, .related-posts, .advertisement, .ad, '
    '.newsletter, .comments, .author-bio, .tags, .breadcrumb'
)
_CREDIT = re.compile(r'^(?:source:|via:|image:|photo:|credits?\b|©)', re.I)


def extract_article_text(
    value: str, *, max_chars: int, junk_pattern: re.Pattern,
    description_fallback_at: int = 0,
) -> str:
    """Prefer the declared article body over a longer page wrapper.

    Short factual paragraphs and list items are retained. Repeated responsive
    copies, navigation links, and adjacent recommendations do not become news.
    """
    if not value or max_chars <= 0:
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    # Decompose descendants first: an ancestor may invalidate a selected child.
    for tag in reversed(soup.select(_JUNK_SELECTORS)):
        tag.decompose()

    def paragraphs(node):
        parts, seen, size = [], set(), 0
        for paragraph in node.find_all(['p', 'li'], limit=500):
            # A list item containing paragraphs otherwise duplicates their text.
            if paragraph.name == 'li' and paragraph.find('p') is not None:
                continue
            text = re.sub(r'\s+', ' ', paragraph.get_text(' ', strip=True)).strip()
            if len(text) < 12 or _CREDIT.match(text) or junk_pattern.search(text):
                continue
            linked = sum(len(a.get_text(' ', strip=True)) for a in paragraph.find_all('a'))
            if linked > len(text) * 0.7:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            parts.append(text)
            size += len(text) + 1
            if size >= max_chars:
                break
        return ' '.join(parts)

    # Do not allow a main/div wrapper with unrelated cards to beat an explicit
    # article body merely by containing more words. Try lower tiers only if empty.
    tiers = (
        '[itemprop="articleBody"], .article-content, .entry-content, .post-content, .article-body',
        'article',
        'main',
        'div, section',
    )
    best = ''
    for selector in tiers:
        best = ''
        for node in soup.select(selector, limit=60):
            text = paragraphs(node)
            if len(text) > len(best):
                best = text
        if best:
            break
    if len(best) < description_fallback_at:
        description = soup.find('meta', property='og:description')
        if description and description.get('content'):
            text = clean_html_fragment(str(description['content']))
            if len(text) > len(best):
                best = text
    return best[:max_chars].strip()


def _host(parsed) -> str:
    return (parsed.hostname or '').casefold().removeprefix('www.')


def _listing_card(anchor):
    """Stay inside one card, including sibling image/text divs.

    Preferring any article/li/section ancestor over a nearer div can select the
    entire listing section: every Read more link then borrows its first title.
    """
    card = None
    for depth, node in enumerate(anchor.parents):
        if depth >= 8:
            break
        if node.name not in ('article', 'li', 'section', 'div'):
            continue
        headings = node.select('h1, h2, h3, h4, h5, h6', limit=2)
        if len(headings) > 1:
            break
        if card is None or headings:
            card = node
        if headings and (node.find('img') is not None or node.name in ('article', 'li')):
            break
    return card


def parse_listing_html(
    html_text: str, *, source_name: str, base_url: str, href_pattern: str,
    limit: int, normalize_image: Callable, normalize_url: Callable,
    lang: str | None = None, title_keywords: tuple[str, ...] | None = None,
) -> list[dict]:
    """Extract source-owned article cards while preserving discovery order."""
    if not html_text or limit <= 0:
        return []
    try:
        base = urlparse(base_url)
        link_re = re.compile(href_pattern, re.I)
        soup = BeautifulSoup(html_text, 'html.parser')
    except (ValueError, re.error):
        return []
    if base.scheme not in ('http', 'https') or not _host(base):
        return []
    output, seen = [], set()
    for anchor in soup.select('a[href]'):
        try:
            parsed = urlparse(urljoin(base_url, str(anchor.get('href') or '').strip()))
            if (parsed.scheme not in ('http', 'https') or parsed.username is not None
                    or _host(parsed) != _host(base) or parsed.port != base.port):
                continue
        except ValueError:
            continue
        # Permalink patterns describe paths, not tracking query parameters.
        # Preserve functional query parameters in the actual fetch URL/key.
        match_url = parsed._replace(query='', fragment='').geturl()
        if not link_re.search(match_url):
            continue
        link = parsed._replace(fragment='').geturl()
        key = normalize_url(link)
        if key in seen:
            continue
        card = _listing_card(anchor)
        title = re.sub(r'\s+', ' ', anchor.get_text(' ', strip=True)).strip()
        if (len(title) < 10 or title.casefold().rstrip(' .»→') in
                ('read more', 'continue reading', 'learn more', 'читать далее', 'подробнее')) and card is not None:
            heading = card.select_one('h1, h2, h3, h4, h5, h6')
            if heading is not None:
                title = re.sub(r'\s+', ' ', heading.get_text(' ', strip=True)).strip()
        if not 10 <= len(title) <= 300:
            continue
        if title_keywords and not any(word.casefold() in title.casefold() for word in title_keywords):
            continue
        summary, images = '', []
        if card is not None:
            paragraph = card.select_one('p')
            if paragraph is not None:
                summary = re.sub(r'\s+', ' ', paragraph.get_text(' ', strip=True)).strip()[:900]
            picture = card.select_one('img[src], img[data-src], img[data-lazy-src]')
            if picture is not None:
                # src often contains a transparent placeholder; prefer lazy source.
                for attr in ('data-src', 'data-lazy-src', 'src'):
                    raw = picture.get(attr)
                    if raw:
                        image = normalize_image(str(raw), link)
                        if image:
                            images.append(image)
                            break
        seen.add(key)
        output.append({
            'title': title[:250], 'link': link, 'summary': summary,
            'source': source_name, 'image': images[0] if images else None,
            'images': images, 'video': None, 'published_parsed': None,
            **({'lang': lang} if lang else {}),
        })
        if len(output) >= limit:
            break
    return output
