"""Аудит качества постов, ноябрь 2026. Примеры пересказаны по постам канала."""

import pytest

import anime_news_bot as bot


# ---------- строка «о чём» ----------

def _about_news(about, **extra):
    return {'title': 'x', '_llm_text': 'x', '_about': about, **extra}


def test_about_retelling_the_body_is_dropped():
    text = ('Анонсировано аниме по манге «Хатта рассудит».\n\n'
            'Аниме основано на манге о девушке-агенте, решающей геополитические вопросы.')
    about = 'Девушка-агент решает взрывоопасные геополитические вопросы, опираясь на знания.'
    assert bot._with_about(text, _about_news(about)) == text


def test_new_plot_line_stays():
    text = 'Анонсировано аниме по манге «Охотник и ведьма».\n\nПремьера в апреле 2027 года.'
    about = 'История об охотнике, который выслеживает ведьму по заказу церкви.'
    assert bot._with_about(text, _about_news(about)).endswith('\n\n' + about)


def test_about_of_another_title_is_dropped():
    """Пост о постере одного тайтла с цитатой о другом из той же новости."""
    text = 'Опубликован постер аниме «Дыши или цвети».\n\nПремьера 26 ноября.'
    about = 'Аниме-сериал по манге «Ночь безумца».'
    assert bot._with_about(text, _about_news(about)) == text
    # Официальное название тайтла поста — не чужой тайтл.
    news = _about_news('Экранизация манги «Ночь безумца» о людях-растениях.', _work_russian='Ночь безумца')
    assert bot._with_about(text, news).endswith('людях-растениях.')


@pytest.mark.parametrize('topic', ['игры', 'кино', 'комиксы', 'прочее'])
def test_no_about_line_outside_anime_and_manga(topic):
    text = 'Сценарий фильма готов.\n\nСъёмки начнутся весной.'
    assert bot._with_about(text, _about_news('Шутер о войне людей с пришельцами.', _llm_topic=topic)) == text


def test_about_line_is_polished():
    """Строка дописывается после чистки текста: неразрывный дефис и ударение."""
    about = bot._about_line(_about_news('Девушка‑агент Сакамо́то спасает мир.', _llm_topic='аниме'))
    assert about == 'Девушка-агент Сакамото спасает мир.'


# ---------- вторая строка повторяет заголовок ----------

@pytest.mark.parametrize('title, para', [
    ('Игра «Бездна» будет весить 212 ГБ, предзагрузка начнётся 10 ноября.',
     'Размер игры объявлен в 212 ГБ, предзагрузка начнётся 10 ноября.'),
    ('Сериал «Немирье» доступен на CHILL: 6 серий.',
     'Сериал «Немирье» из 6 эпизодов уже доступен на платформе CHILL.'),
    ('Запланирован 3 сезон «Рубеж Шангри-Ла» на 10 января 2027.',
     'Третий сезон аниме «Рубеж Шангри-Ла» (Shangri-La Frontier) стартует 10 января 2027 года.'),
    ('Предзагрузка «Бездны» начнётся 10 ноября, вес 212 ГБ.',
     'Размер «Бездны» (Abyss Online) составит 212 ГБ, предзагрузка 10 ноября.'),
    ('В январе начнётся демонстрация аниме по ранобэ «Старый авантюрист».',
     'Демонстрация аниме-адаптации ранобэ стартует в январе.'),
])
def test_retold_title_is_dropped(title, para):
    assert bot._drop_repetitive_paragraphs(title, [para]) == []


def test_retold_start_of_sentence_is_cut_fact_stays():
    out = bot._drop_repetitive_paragraphs('Вышел новый трейлер аниме «Лунный вампир».',
                                          ['Показан новый трейлер, аниме выйдет в январе 2027 года.'])
    assert out == ['Аниме выйдет в январе 2027 года.']


@pytest.mark.parametrize('para', [
    'Показан постер, в котором видна героиня Мэй.',              # придаточное без начала не читается
    'Студия Voil показала постер, премьера запланирована на 2027 год.',   # новое имя в начале
])
def test_sentence_start_with_new_fact_stays(para):
    assert bot._drop_repetitive_paragraphs('Вышел постер к аниме «Лунный вампир».', [para]) == [para]


# ---------- пустые фразы ----------

@pytest.mark.parametrize('para, expected', [
    ('Постер с Каной появился, дата 4 сезона пока не объявлена.', 'Постер с Каной появился.'),
    ('Продолжение манги анонсировано, дата не указана.', 'Продолжение манги анонсировано.'),
    ('Опубликованы дизайны персонажей, однако в объявлении не указаны каст, студия и дата выхода.',
     'Опубликованы дизайны персонажей.'),
    ('Показаны постер и промо-ролик, детали кастинга, студии и даты выхода не раскрыты.',
     'Показаны постер и промо-ролик.'),
    ('Режиссёр не назван, но известно, что сценарий пишет Ито.',
     'Режиссёр не назван, но известно, что сценарий пишет Ито.'),
    ('Сериал выйдет весной, студия MAPPA.', 'Сериал выйдет весной, студия MAPPA.'),
])
def test_empty_claims(para, expected):
    assert bot._drop_empty_claims(para) == expected


# ---------- жаргон и мелкие дефекты ----------

@pytest.mark.parametrize('raw, expected', [
    ('Анонсировано тизер-визуал и тизер-PV аниме «Икс».', 'Опубликованы постер и тизер аниме «Икс».'),
    ('Показаны визуал и промо-ролик.', 'Показаны постер и промо-ролик.'),
    ('Время позволило улучшить визуал и детали.', 'Время позволило улучшить визуал и детали.'),
    ('Показан новый визуал аниме.', 'Показан новый постер аниме.'),
    ('Вышли тизерные визуалы.', 'Вышли постеры.'),
    ('Manga Sakamoto Holidays близится к финалу.', 'Манга Sakamoto Holidays близится к финалу.'),
    ('Manga Plus открыла раздел.', 'Manga Plus открыла раздел.'),
    ('Spin-off манги Sakamoto.', 'Спин-офф манги Sakamoto.'),
    ('Слух: адаптация light novel «Сонмо».', 'Слух: адаптация ранобэ «Сонмо».'),
    ('Новый постер 2 сезона ««Волчица и пряности»».', 'Новый постер 2 сезона «Волчица и пряности».'),
    ('Фильм «Добро пожаловать в «Город»» выйдет.', 'Фильм «Добро пожаловать в «Город»» выйдет.'),
    ('Обложка 2 тома манги «Дом».\n#НовостиWeeklyJump', 'Обложка 2 тома манги «Дом».'),
    ('Анонс GTA Vl не поднял продажи.', 'Анонс GTA VI не поднял продажи.'),
    ('Манга ВЕДЬМ НАДЗОР выйдет. США и МВД.', 'Манга Ведьм надзор выйдет. США и МВД.'),
    ('Продлён сезон «АБДУКЦИЯ».', 'Продлён сезон «Абдукция».'),
    ('Вот и всё - вышла ПОСЛЕДНЯЯ серия.', 'Вот и всё - вышла ПОСЛЕДНЯЯ серия.'),
    ('Анонсировано аниме «Тeпленько».', 'Анонсировано аниме «Тепленько».'),        # латинская «e»
    ('Новый промо-ролик (PV) к тому.', 'Новый промо-ролик к тому.'),
])
def test_jargon_and_typos(raw, expected):
    assert bot._polish_post_text(raw) == expected


@pytest.mark.parametrize('raw, expected', [
    ('Анонсировано кроссовер двух тайтлов.', 'Анонсирован кроссовер двух тайтлов.'),
    ('Анонсировано аниме-адаптация ранобэ «Икс».', 'Анонсирована аниме-адаптация ранобэ «Икс».'),
    ('Анонсирована третья сезон аниме «Икс».', 'Анонсирован третий сезон аниме «Икс».'),
    ('Анонсирован аниме King\'s Proposal.', 'Анонсировано аниме King\'s Proposal.'),
    ('Вышел постер и трейлер к аниме «Икс».', 'Вышли постер и трейлер к аниме «Икс».'),
    ('Выдали постер ко 2 сезону «Икс».', 'Опубликован постер ко 2 сезону «Икс».'),
    ('Выдали дату 4 сезона «Икс».', 'Объявлена дата 4 сезона «Икс».'),
    ('Выданы постеры к аниме «Икс».', 'Опубликованы постеры к аниме «Икс».'),
    ('Объявлен постер аниме «Икс».', 'Опубликован постер аниме «Икс».'),
    ('Вышел 1-й глава манги «Икс».', 'Вышла 1-я глава манги «Икс».'),
    ('Подтверждено вторая часть.', 'Подтверждена вторая часть.'),
])
def test_title_verb_agrees_with_subject(raw, expected):
    assert bot._polish_post_text(raw) == expected


@pytest.mark.parametrize('title', [
    'Вышел трейлер и объявлена дата 3 сезона «Икс».',     # второе «и» — не подлежащее
    'Анонсировано продолжение аниме «Икс».',
    'Анонсирован 2 сезон «Икс».',
    'Запланированы 3 сезона «Икс».',                      # «сезона» — не именительный
    'Опубликован праздничный постер аниме «Икс».',
    'Представлен ключевой постер аниме.',
    'Анонсирован «Ведьмнадзор».',
    'Анонсировано новое аниме «Икс».',
    'Вышел ночной трейлер аниме.',                         # согласованное «-ой» не становится «-ый»
    'Вышел постер, и трейлер покажут позже.',             # после запятой — другое предложение
])
def test_correct_titles_are_untouched(title):
    assert bot._polish_post_text(title) == title


# ---------- значок и пометка обновления ----------

def test_story_update_has_no_label_and_the_usual_emoji(monkeypatch):
    """Значок 🔄 и пометку «Обновление:» админы стирали с постов руками
    (ноябрь 2026): обновление выходит с обычным значком события."""
    monkeypatch.setattr(bot, 'POST_STYLE', 'rich')
    monkeypatch.setattr(bot, 'editorial_glossary', None)
    monkeypatch.setattr(bot, 'entity_memory', None)
    news = {'_story_update_of': 'abc'}
    text = bot._apply_editorial_rules('Слух: бюджет фильма превысил 100 млн.', news)
    assert not text.startswith('Обновление')
    assert bot._post_html(text, 1024, news).startswith('👀 <b>Слух: бюджет')
    # Пометка из поста, ждавшего в ветке со старой версии, тоже снимается.
    assert bot._post_html('Обновление: вышел трейлер.', 1024, news).startswith('🎬 <b>Вышел трейлер.')
    assert bot._post_html('🔄 Обновление: вышел трейлер.', 1024, news).startswith('🎬 <b>Вышел трейлер.')
    assert bot._post_html('Обновление:', 1024, news) == '✨ <b>Обновление:</b>'   # пустым заголовок не станет


@pytest.mark.parametrize('topic, emoji', [('игры', '🎮'), ('манга', '📚'), ('аниме', '📺')])
def test_release_emoji_follows_topic(topic, emoji):
    assert bot._post_emoji('Новость', {'_llm_kind': 'релиз', '_llm_topic': topic}) == emoji


# ---------- правила канала ----------

@pytest.mark.parametrize('title', ['Официальный арт к дню рождения Дофламинго.', 'Арт ко дню рождения Луффи.',
                                   'Постер в день рождения героя.'])
def test_birthday_art_is_noise(title):
    assert bot._ANNIVERSARY_SOFT_RE.search(title) and not bot._anniversary_is_news(title)


@pytest.mark.parametrize('title, western', [
    ('Сценарий экранизации Gears of War готов.', True),
    ('Лайв-экшн сериал по PUBG снимет Гловер.', True),
    ('Запущено производство фильма по вселенной Cyberpunk.', True),
    ('Вышла игра по фильму «Мстители».', False),
    ('GTA VI будет весить 212 ГБ.', False),
])
def test_game_adaptation_counts_as_western_screen(title, western):
    assert bot._is_western_screen({'_llm_topic': 'игры', '_llm_text': title}) is western


# ---------- повторы: латинское и русское имя одного тайтла ----------

def test_alias_pair_links_latin_and_russian_posts(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'work_memory', bot.WorkMemory(tmp_path / 'works.json'))
    monkeypatch.setattr(bot, 'work_aliases', bot.WorkAliases(tmp_path / 'aliases.json'))
    monkeypatch.setattr(bot, 'work_titles', None)
    monkeypatch.setattr(bot, 'anilist', None)
    bot._work_remember({'title': 'Опубликован постер ко 2 сезону «Перерождение в меч».',
                        'link': 'https://e/1'}, 'bot')
    # Потом админ написал пару «русское (латинское)» — бот её запомнил; пост
    # бота, вышедший раньше, связывается с латинским именем уже при проверке.
    bot._work_remember({'title': 'Кадры 2 сезона «Перерождение в меч» (Reincarnated as a Sword)',
                        'link': 'https://t.me/x/1'}, 'admin')
    bot.work_memory._items.pop()                     # сам пост админа — не в счёт
    later = {'title': 'Вышли новый постер и трейлер второго сезона аниме Reincarnated as a Sword II.',
             'link': 'https://e/2'}
    assert bot._work_repeat_hit(later)
    # Пара пережила перезапуск.
    assert 'name:reincarnatedasasword' in bot.WorkAliases(tmp_path / 'aliases.json').expand(
        ['ru:перерождениевмеч'])


def test_digest_names_are_not_glued_together(tmp_path):
    """Только явные пары: два тайтла подборки не становятся одним."""
    news = {'title': 'Crunchyroll добавит дубляж Black Clover и «Волчица и пряности».'}
    assert bot._work_title_pairs_in(news) == []
    # Латиница с обеих сторон — два латинских имени, а не перевод.
    assert bot._work_title_pairs_in({'title': '«Black Clover» (Burakku Kuroba) вернётся.'}) == []


def test_latin_head_of_mixed_name_meets_full_latin_name(monkeypatch):
    monkeypatch.setattr(bot, 'work_titles', None)
    monkeypatch.setattr(bot, 'anilist', None)
    old = bot._work_identity_keys({'title': 'Премьера аниме «Death Stranding: Выход смерти» в 2027 году.'})
    new = bot._work_identity_keys({'title': 'Показаны постер и трейлер аниме «Death Stranding».'})
    assert bot._work_keys_meet(new, old)
