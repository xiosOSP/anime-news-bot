"""Bounded, network-free extraction helpers shared by feed and article parsers."""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
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
# Подпись под картинкой — не текст новости. WordPress кладёт её прямо в
# описание RSS первой строкой, и в пост уезжало «Courtesy of Netflix
# Cyberpunk: Edgerunners is coming back…»: кредит фото склеивался с первой
# фразой новости, и отделить его потом было уже нечем.
_HIDDEN = frozenset({'script', 'style', 'noscript', 'template', 'svg', 'figcaption'})


class _FragmentText(HTMLParser):
    """Keep inline typography, separate block boundaries, omit executable text."""

    def __init__(self, *, paragraphs: bool = False):
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.hidden: list[str] = []
        self.separator = '\n' if paragraphs else ' '

    def handle_starttag(self, tag, attrs):
        if tag in _HIDDEN:
            if not self.hidden:
                self.parts.append(' ')
            self.hidden.append(tag)
        elif not self.hidden and tag in _BLOCKS:
            self.parts.append(self.separator)

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                index = len(self.hidden) - 1 - self.hidden[::-1].index(tag)
                del self.hidden[index:]
            return
        if tag in _BLOCKS:
            self.parts.append(self.separator)

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


def message_html_text(value: str) -> str:
    """Preserve real paragraphs, never split a sentence at inline markup."""
    parser = _FragmentText(paragraphs=True)
    parser.feed(value or '')
    parser.close()
    text = html.unescape(''.join(parser.parts)).replace('\u200b', '')
    return '\n'.join(re.sub(r'[^\S\n]+', ' ', line).strip()
                     for line in text.splitlines()).strip()


_BYLINE = re.compile(
    r'^(?:published(?:\s+on)?|posted(?:\s+on)?|updated(?:\s+on)?|'
    r'опубликовано|обновлено)\s+(?:\d|[A-Z][a-z]+\s+\d)', re.I)


# Кнопки-призывы пресс-релизов, склеенные с текстом: у GKIDS описание
# начиналось с «VIEW TRAILER HERETICKETS ON SALE NOW». Ищем только капслоком:
# «билеты уже в продаже» строчными — это факт новости, а капслоком — кнопка.
_CALL_TO_ACTION = re.compile(
    r'\b(?:VIEW|WATCH|SEE)\s+(?:THE\s+)?(?:NEW\s+)?TRAILER\s+HERE'
    r'|(?:GET\s+)?TICKETS\s+(?:ARE\s+)?(?:ON\s+SALE\s+NOW|HERE)\b'
    r'|\b(?:CLICK|READ\s+MORE|LEARN\s+MORE)\s+HERE\b')


def clean_editorial_source(value: str) -> str:
    """Remove standalone CMS bylines, not dates or names inside news facts."""
    lines = []
    for line in str(value or '').splitlines():
        line = re.sub(r'[^\S\n]+', ' ', line).strip()
        if _BYLINE.match(line):
            continue
        line = re.sub(r'\s{2,}', ' ', _CALL_TO_ACTION.sub(' ', line)).strip()
        if not line:
            continue
        line = re.sub(r'\s+([,.;!?])', r'\1', line)
        line = re.sub(r'«\s+', '«', line)
        line = re.sub(r'\s+»', '»', line)
        lines.append(line)
    return '\n'.join(lines).strip()


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
            text = clean_html_fragment(str(paragraph))
            if (len(text) < 12 or _CREDIT.match(text) or _BYLINE.match(text)
                    or junk_pattern.search(text)):
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


_MONTHS = {name: number for number, names in enumerate((
    ('jan', 'january'), ('feb', 'february'), ('mar', 'march'), ('apr', 'april'),
    ('may',), ('jun', 'june'), ('jul', 'july'), ('aug', 'august'),
    ('sep', 'sept', 'september'), ('oct', 'october'), ('nov', 'november'),
    ('dec', 'december')), start=1) for name in names}
_BYLINE_DATE = re.compile(
    r'^(?:published|posted|updated)(?:\s+on)?\s+([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(20\d\d)\b', re.I)
_URL_DATE = re.compile(r'/(20\d\d)/(\d\d)/(\d\d)/')


def _struct_or_none(year: int, month: int, day: int, hour: int = 0, minute: int = 0):
    try:
        return datetime(year, month, day, hour, minute).timetuple()
    except (TypeError, ValueError):
        return None


def listing_published(card, link: str):
    """Дата карточки на странице-списке, как её отдают RSS (struct_time, UTC).

    У страниц-списков нет поля даты, и все карточки считались свежими: фильтр
    возраста их пропускал, и в канал могла уйти апрельская новость, если её
    ссылка выпала из истории отправленного. Дату ищем в трёх местах по
    убыванию точности: тег <time>, подпись «Posted Aug 24, 2026», дата в
    адресе /2026/09/19/. Не нашли — None, как и раньше: лучше пропустить
    старую новость, чем потерять свежую из-за разметки.
    """
    if card is not None:
        stamp = card.select_one('time[datetime]')
        if stamp is not None:
            raw = str(stamp.get('datetime') or '').strip()
            try:
                moment = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            except ValueError:
                moment = None
            if moment is not None:
                if moment.tzinfo is not None:
                    moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
                return moment.timetuple()
        for paragraph in card.select('p, span, div'):
            text = re.sub(r'\s+', ' ', paragraph.get_text(' ', strip=True))
            match = _BYLINE_DATE.match(text)
            if match and match.group(1).casefold() in _MONTHS:
                return _struct_or_none(int(match.group(3)), _MONTHS[match.group(1).casefold()],
                                       int(match.group(2)))
    match = _URL_DATE.search(str(link or ''))
    if match:
        return _struct_or_none(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return None


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
            # Первый абзац карточки часто служебный: у Yen Press это «Posted
            # Aug 24, 2026 by …», и описанием новости становилась подпись.
            for paragraph in card.select('p'):
                text = re.sub(r'\s+', ' ', paragraph.get_text(' ', strip=True)).strip()
                if text and not _BYLINE.match(text):
                    summary = text[:900]
                    break
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
            'images': images, 'video': None,
            'published_parsed': listing_published(card, link),
            **({'lang': lang} if lang else {}),
        })
        if len(output) >= limit:
            break
    return output
