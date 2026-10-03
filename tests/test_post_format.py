"""Вид и язык постов как у живых админов канала.

Замер по 527 постам канала: у админов эмодзи + жирный заголовок + одна-две
фразы, названия тайтлов по-русски; у бота — сплошной текст, ромадзи и даже
иероглифы («葬送のフリーレン»), а реакций на его посты в пять раз меньше.
"""
import re
from types import SimpleNamespace as NS

import pytest

import anime_news_bot as bot
import llm_protocol as llm

FRIEREN = {'_work_titles_ru': {'葬送のフリーレン': 'Провожающая в последний путь Фрирен'}}
INTON = {'_work_name': 'Saikyou Mahoushi no Inton Keikaku',
         '_work_russian': 'План отхода от дел великого мастера магии'}
GRAND = {'_work_name': 'Grand Blue', '_work_russian': 'Необъятный океан'}


# ------------------------------------------------- названия тайтлов

def test_japanese_title_becomes_the_official_russian_one():
    out = bot._russify_titles('Премьера 2 сезона «葬送のフリーレン».\n\nПоказ «葬送のフリーレン».', FRIEREN)
    assert out == ('Премьера 2 сезона «Провожающая в последний путь Фрирен».\n\n'
                   'Показ «Провожающая в последний путь Фрирен».')


def test_romaji_title_is_replaced_without_gluing_words():
    out = bot._russify_titles('Анонсирован 2 сезон Saikyou Mahoushi no Inton Keikaku и всё.', INTON)
    assert out == 'Анонсирован 2 сезон «План отхода от дел великого мастера магии» и всё.'


def test_short_latin_original_stays_in_brackets_once():
    out = bot._russify_titles('Вышел трейлер «Grand Blue».\n\nGrand Blue вернётся.', GRAND)
    assert out == 'Вышел трейлер «Необъятный океан» (Grand Blue).\n\n«Необъятный океан» вернётся.'


def test_model_did_it_right_and_nothing_doubles():
    text = 'Вышел трейлер «Необъятный океан» (Grand Blue).'
    assert bot._russify_titles(text, GRAND) == text


def test_japanese_original_in_brackets_is_dropped_but_facts_stay():
    out = bot._russify_titles('Премьера «Рус» (葬送のフリーレン 第2期) и (4-я серия «鬼太郎»).', {})
    assert out == 'Премьера «Рус» и (4-я серия «鬼太郎»).'


def test_switch_off_keeps_text(monkeypatch):
    monkeypatch.setattr(bot, 'settings', NS(russian_titles=False))
    assert bot._russify_titles('«葬送のフリーレン»', FRIEREN) == '«葬送のフリーレン»'


def test_hint_lists_every_known_title():
    news = dict(INTON, _work_titles_ru={'葬送のフリーレン': 'Провожающая в последний путь Фрирен'})
    hint = bot._work_title_hint(news)
    assert '«План отхода от дел великого мастера магии»' in hint
    assert '«Провожающая в последний путь Фрирен»; в источнике — 葬送のフリーレン' in hint


def test_every_title_of_the_news_is_resolved(monkeypatch):
    class Titles:
        known = {'葬送のフリーレン': 'Провожающая в последний путь Фрирен',
                 'ゲゲゲの鬼太郎': 'Китаро с кладбища'}
        looked = []

        def cached(self, name):
            return None

        def lookup(self, name):
            self.looked.append(name)
            return 'k:' + name if name in self.known else None

        def russian(self, name):
            return self.known.get(name, '')

        def cached_by_stem(self, name):
            return None, ''

        def flush(self):
            pass
    titles = Titles()
    monkeypatch.setattr(bot, 'work_titles', titles)
    monkeypatch.setattr(bot, 'feature_enabled', lambda name: True)
    monkeypatch.setattr(bot, 'sent_links', None)
    monkeypatch.setattr(bot, 'anilist', None)
    item = {'title': 'Премьера «葬送のフリーレン»', 'link': 'https://x/1',
            'summary': 'Также покажут «ゲゲゲの鬼太郎» и ток-шоу.'}
    bot._annotate_work_keys([item], budget=10)
    assert item['_work_titles_ru'] == titles.known


# ------------------------------------------------- японское письмо не выходит

def test_post_with_japanese_letters_is_held_back(monkeypatch):
    news = {'title': 'x', '_llm_text': 'Премьера в TOHOシネマズ日比谷.', 'link': 'https://x/1'}
    monkeypatch.setattr(bot, 'format_news_short', lambda n: n['_llm_text'])
    assert bot._left_untranslated(news)
    news['_llm_text'] = 'Премьера в TOHO Cinemas Hibiya.'
    assert not bot._left_untranslated(news)
    assert not bot._left_untranslated({'_edited_text': 'Админ оставил 葬送', 'title': 'x'})


# ------------------------------------------------- заголовок и пересказ

def test_title_loses_tags_and_shouting():
    assert bot._tidy_title_line('Отчёт о показе «Хлебный вор»! [Интервью]\n\nТекст!') == \
        'Отчёт о показе «Хлебный вор».\n\nТекст!'
    assert bot._tidy_title_line('【速報】Анонс') == 'Анонс'


def test_source_retelling_is_rejected():
    assert llm._editorial_rejection('src', 'Сцена из фильма', 'В источнике TG: QewbsNews упомянута сцена.') \
        == 'source_meta'
    assert llm._editorial_rejection('src', 'Вышел трейлер аниме', 'Премьера в октябре.') == ''


def test_prompt_asks_for_short_russian_posts():
    prompt = llm.LLM_SYSTEM_PROMPT
    assert 'иероглифы' in prompt and 'Поливанова' in prompt and 'восклицательных' in prompt
    assert llm.LLM_SUMMARY_MAX <= 450 and llm.LLM_MAX_PARAGRAPHS == 2


# ------------------------------------------------- вид поста

def test_rich_post_has_emoji_bold_title_and_blank_line():
    out = bot._post_html('Анонсировано аниме «Власть книжного червя».\nДата пока неизвестна.', 1024,
                         {'_llm_kind': 'анонс'})
    assert out == '🔥 <b>Анонсировано аниме «Власть книжного червя».</b>\n\nДата пока неизвестна.'


@pytest.mark.parametrize('title, news, emoji', [
    ('Слух: сериал отложен', {}, '👀'),
    ('Вышел тизер 2 сезона', {}, '🎬'),
    ('Объявлен фильм', {}, '🔥'),
    ('Манга завершилась', {}, '📚'),
    ('Стартует показ', {}, '📺'),
    ('Новость дня', {'_llm_topic': 'игры'}, '🎮'),
    ('Новость дня', {}, '✨'),
    ('Обновление: перенос', {}, '🔄'),
    ('Новость', {'_llm_kind': 'трейлер'}, '🎬'),
])
def test_emoji_follows_the_event(title, news, emoji):
    assert bot._post_emoji(title, news) == emoji


def test_admin_emoji_is_not_doubled_and_html_is_escaped():
    out = bot._post_html('🔥 Свой <заголовок> & всё', 1024, {})
    assert out == '<b>🔥 Свой &lt;заголовок&gt; &amp; всё</b>'


def test_rich_post_fits_the_telegram_limit():
    out = bot._post_html('Заголовок.\n\n' + 'Длинный текст. ' * 200, 1024, {})
    assert len(re.sub(r'<[^>]+>', '', out)) <= 1024


def test_plain_style_is_the_old_look(monkeypatch):
    monkeypatch.setattr(bot, 'POST_STYLE', 'plain')
    assert bot._post_html('Заголовок.\nТекст.', 1024, {}) == bot._escape_to_limit('Заголовок.\nТекст.', 1024)


def test_channel_and_thread_posts_use_the_rich_form():
    import inspect
    source = inspect.getsource(bot)
    assert source.count('_post_html(') >= 8
    assert 'caption = _escape_to_limit(text, TG_CAPTION_LIMIT)' not in source


def test_later_mentions_after_a_correct_russian_title_get_no_second_bracket():
    out = bot._russify_titles('Вышел трейлер «Необъятный океан» (Grand Blue).\n\nGrand Blue вернётся.', GRAND)
    assert out == 'Вышел трейлер «Необъятный океан» (Grand Blue).\n\n«Необъятный океан» вернётся.'


def test_editorial_rules_apply_titles_and_tidy_heading(monkeypatch):
    monkeypatch.setattr(bot, 'feature_enabled', lambda name: False)
    out = bot._apply_editorial_rules('Премьера «葬送のフリーレン»! [Интервью]\n\nТекст.', FRIEREN)
    # Очистка сжимает пустую строку в перенос — отступ ставит _post_html при выводе.
    assert out == 'Премьера «Провожающая в последний путь Фрирен».\nТекст.'
