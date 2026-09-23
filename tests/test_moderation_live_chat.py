"""Модерация на живых аниме-чатах: что показал прогон реальных комментариев.

Около 9,7 тыс. реплик из комментариев аниме-каналов Telegram и Shikimori
прогнаны через локальный слой бота. Сами реплики в репозиторий не попали —
это сообщения живых людей; ниже их обезличенные пересказы той же формы.

Две группы тестов. Первая — ошибки, найденные на данных: из 90 писем
«короткое сообщение со ссылкой» 83 были обычным разговором, реакция на спам-бота
стоила предупреждения, а самоирония «я … пидорас» — мута. Вторая — правила,
противоречившие собственному своду чата, который бот передаёт модели:
«политика в сюжете — не политика», «игровой рейд и война в сюжете — не
raid», «похожего корня недостаточно». На данных они не встретились, но
каждая такая ошибка — наказание невиновного.
"""
import pytest

import anime_news_bot as bot
import moderation_rules as rules


def _local(text: str, *, reply: bool = False):
    # Свой чат на каждый вызов: частотные проверки не должны мешать тексту.
    return bot._mod_local_check(abs(hash(text)) % 10**9 + 1, 1, text, reply_to_user=reply)


# ---------- ссылки: подозрение только там, где есть что проверять ----------

@pytest.mark.parametrize('text', [
    'https://youtu.be/abc123XYZ?si=share',
    'вот опенинг https://www.youtube.com/watch?v=abc123',
    'https://music.youtube.com/watch?v=abc123',
    'этот персонаж https://shikimori.one/characters/1-name',
    'спин-офф с https://shikimori.io/characters/2-name',
    'https://coub.com/view/abc1',
    'замена на видео https://vk.com/video-1_2',
    'Автор арта: https://twitter.com/artist',
    'отсылка к https://ru.kinorium.com/187028/',
    'можно почитать тут https://ru.wikipedia.org/wiki/Исекай',
    'www.shikimori.one/animes/1',
])
def test_link_to_the_conversation_is_not_a_spam_suspicion(text):
    """Клип, опенинг, карточка персонажа — часть разговора.

    На живых комментариях 83 из 90 писем «короткое сообщение со ссылкой» были
    обычным разговором, а каждое тратило вызов модели или будило админа.
    """
    assert _local(text) is None


@pytest.mark.parametrize('text', [
    '@someone_nick работай',
    'я записался, и мой бро @friend_nick тоже)',
])
def test_member_mention_is_not_a_link(text):
    assert _local(text) is None


@pytest.mark.parametrize('text', [
    'https://kinogo-free.example/98535-film.html',      # незнакомый сайт
    't.me/some_channel',
    'https://t.me/some_channel/12',
    'https://va.media.tumblr.com/tumblr_abc.mp4',        # 18+ бывает и там
    'https://gelbooru.com/index.php?page=post',
    'https://youtube.com.promo-site.example/watch',      # притворяется YouTube
    'https://vk.com/club123',                            # группа, а не видео
    '@casino_win_bot',                                   # бот, не участник
    'подписывайтесь на мой канал https://youtube.com/@me',
    'заходите на мой стрим https://youtu.be/abc',
])
def test_other_links_still_reach_the_second_level(text):
    # quiet: модель, если она есть, посмотрит ссылку; без модели письма админу нет.
    verdict = _local(text)
    assert verdict == {'category': 'spam', 'confident': False, 'quiet': True,
                       'reason': 'короткое сообщение со ссылкой; нужен контекст'}


def test_promo_on_you_plural_is_promo():
    """«Заходите» — такой же призыв, как «заходи»: раньше ловилась только форма на «ты»."""
    verdict = _local('заходите к нам https://t.me/+AbCdEfGh')
    assert verdict['category'] == 'spam' and verdict['confident'] is True


# ---------- грубость не к человеку ----------

@pytest.mark.parametrize('text', [
    'ааа спамбот пошел нахуй',
    'бот, пошел нахуй',
    'пошел нахуй, спамер',
    'Заткнись и возьми мои деньги',
    'заткнись и бери мои деньги, студия',
])
def test_rudeness_to_a_spam_bot_or_in_a_meme_is_not_aggression(text):
    """Человек, которого разозлил спам-бот, предупреждения не заслужил."""
    verdict = rules.check_text(text)
    assert verdict is None or verdict.category != 'aggression'


@pytest.mark.parametrize('text', [
    'пошел нахуй',
    'ты бот, пошел нахуй',          # ботом обзывают человека
    'работа, пошел нахуй',          # «работа» — не бот
    'заткнись уже',
    'заткнись и не пиши мне',
])
def test_rudeness_to_a_person_is_still_aggression(text):
    verdict = rules.check_text(text)
    assert verdict is not None and verdict.category == 'aggression'


# ---------- правила, противоречившие своду чата ----------

@pytest.mark.parametrize('text', [
    'в Кагуе лучшая арка — выборы президента студсовета',
    'выборы президента школьного совета затянули',
    'голосуйте за Кагую как президента студсовета',
])
def test_school_council_election_is_the_plot(text):
    assert rules.check_text(text) is None


@pytest.mark.parametrize('text', [
    'выборы президента России',
    'голосуйте за нашего президента',
])
def test_real_election_is_still_politics(text):
    verdict = rules.check_text(text)
    assert verdict is not None and verdict.category == 'politics' and verdict.confident


def test_plot_about_someone_elses_family_is_not_a_family_insult():
    """Без «твою/вашу» фраза о чужой семье — это пересказ сюжета."""
    assert rules.check_text('он трахал сестру всю мангу, ну и сюжет') is None


@pytest.mark.parametrize('text', ['я ебал твою мать', 'трахал маму твою', 'Я выебу твою мать'])
def test_insult_to_the_interlocutors_family_is_still_family(text):
    verdict = rules.check_text(text)
    assert verdict is not None and verdict.category == 'family'


@pytest.mark.parametrize('text', [
    'атакуем группу разведчиков',
    'набег на группу титанов в финале',
])
def test_attack_in_the_plot_is_not_a_raid(text):
    assert rules.check_text(text) is None


@pytest.mark.parametrize('text', [
    'атакуем их канал',
    'набег на этот чат',
    'атакуем https://t.me/some_chat',
    'рейдим канал',
    'заспамим группу',
])
def test_attack_on_a_chat_is_still_a_raid(text):
    verdict = rules.check_text(text)
    assert verdict is not None and verdict.category == 'raid'


@pytest.mark.parametrize('text', ['хачу второй сезон', 'ХАЧУ продолжение!!!', 'хачю спать'])
def test_joke_spelling_of_want_is_not_a_slur(text):
    """«Хачу» — шуточное «хочу». Совпадение со словом в дательном падеже стоило мута сразу."""
    assert _local(text) is None


@pytest.mark.parametrize('text', ['понаехали хачи', 'ненавижу хачей', 'хачу тупому объясни'])
def test_the_slur_itself_is_still_hate(text):
    assert _local(text) == {'category': 'hate', 'confident': True}


# ---------- самоирония ----------

@pytest.mark.parametrize('text', ['я многонациональный пидорас', 'я тот ещё пидор', 'я просто хач'])
def test_slur_about_oneself_goes_to_review_not_to_a_mute(text):
    """Свод правил чата: «я гей» — не нарушение, самоирония разрешена.

    На живых комментариях реплика «я … пидорас» в ответ собеседнику стоила
    мута сразу. Слово не пропадает: сообщение уходит на второй уровень.
    """
    assert _local(text, reply=True) == {'category': '', 'confident': False}


@pytest.mark.parametrize('text', [
    'я думаю он пидорас',            # «я» говорит, но слово — о другом
    'я знаю ты пидор',
    'я пидор, а ты пидорас',         # о себе — одно, собеседнику — другое
])
def test_slur_about_someone_else_is_still_hate(text):
    assert _local(text, reply=True) == {'category': 'hate', 'confident': True}
