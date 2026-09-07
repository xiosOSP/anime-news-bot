"""Пачка новостей за один вызов модели вместо вызова на каждую.

Все бесплатные пулы (orcarouter, Mistral, OpenRouter) отвечают 429 примерно
одновременно: это общие лимиты, поделённые на всех пользователей сервиса.
Единственный оставшийся рычаг — просить у модели меньше. Цикл из десяти
новостей стоил десяти запросов; здесь проверяется, что он стоит одного-двух и
что при этом ни одна новость не теряется.
"""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

import anime_news_bot as bot


def _reply(payload):
    """Ответ провайдера с готовым JSON внутри."""
    return _reply_text(json.dumps(payload, ensure_ascii=False))


def _reply_text(text):
    """Ответ провайдера с произвольным текстом — в том числе с битым JSON."""
    return MagicMock(status_code=200, text='',
                     json=lambda: {'choices': [{'message': {'content': text}}]})


def _item(idx):
    return {'id': idx, 'topic': 'аниме', 'kind': 'новость',
            'title': f'Заголовок {idx}', 'summary': f'Суть новости номер {idx}.',
            'tags': [f'#тег{idx}']}


def _news(idx):
    """Новость без ссылки: за статьёй ходить некуда, сеть тесту не нужна."""
    return {'title': f'Headline number {idx}',
            'summary': f'Body of the news number {idx}.',
            'source': 'Polygon', 'lang': 'en'}


def _queued(idx):
    """То же, но пригодное для очереди: без ссылки и картинки её не пускают."""
    return dict(_news(idx), link=f'https://x.test/{idx}',
                images=[f'https://x.test/{idx}.jpg'])


@pytest.fixture
def llm(tmp_path, monkeypatch):
    """Настроенная модель, чистые счётчики и пустой кеш разборов."""
    monkeypatch.setattr(bot, 'LLM_API_KEY', 'k')
    monkeypatch.setattr(bot, 'LLM_BASE_URL', 'https://x/v1')
    monkeypatch.setattr(bot, 'LLM_MODEL', 'm')
    monkeypatch.setattr(bot, 'LLM_MIN_INTERVAL', 0)
    monkeypatch.setattr(bot, 'LLM_DAILY_LIMIT', 100)
    monkeypatch.setattr(bot, 'LLM_BATCH_SIZE', 4)
    monkeypatch.setattr(bot, '_llm_disabled_runtime', False)
    monkeypatch.setattr(bot, '_llm_fail_streak', 0)
    monkeypatch.setattr(bot, '_llm_editorial_cache', {})
    monkeypatch.setattr(bot, 'settings', bot.BotSettings(tmp_path / 's.json'))
    bot._pending_admin_alerts.clear()
    return bot


# ---------- главное: одна пачка вместо вызова на каждую новость ----------

def test_pack_of_four_costs_one_call(llm):
    """Ради этого всё и делалось: четыре новости — один запрос, а не четыре."""
    pack = [_news(i) for i in range(1, 5)]
    answer = _reply({'items': [_item(i) for i in range(1, 5)]})
    with patch.object(llm.requests, 'post', return_value=answer) as post:
        assert asyncio.run(llm._llm_enrich_batch(pack)) == 4
        results = [asyncio.run(llm._llm_enrich(news)) for news in pack]
    assert results == ['ok'] * 4
    assert post.call_count == 1, 'обогащение снова платит за каждую новость'
    assert llm.settings.llm_calls_today == 1


def test_batched_news_gets_the_same_text_as_a_single_call(llm):
    """Экономия не должна идти за счёт постов: разбор применяется тот же.

    Пакетный путь кладёт ответ в кеш, а весь разбор — заголовок, текст, теги —
    остаётся общим с одиночным вызовом. Если развести их на две ветки, правила
    рано или поздно разъедутся, и посты из пачки станут хуже.
    """
    pack = [_news(1), _news(2)]
    answer = _reply({'items': [_item(1), _item(2)]})
    with patch.object(llm.requests, 'post', return_value=answer):
        asyncio.run(llm._llm_enrich_batch(pack))
        assert asyncio.run(llm._llm_enrich(pack[0])) == 'ok'
    assert 'Заголовок 1' in pack[0]['_llm_text']
    assert 'Суть новости номер 1' in pack[0]['_llm_text']
    assert pack[0]['_llm_tags'] == '#тег1'


def test_answers_go_to_the_right_news(llm):
    """Ответ сопоставляется по id, а не по порядку.

    Модель вправе переставить элементы местами. Без сопоставления по id пост
    получил бы чужой заголовок — то есть выдуманную новость.
    """
    pack = [_news(1), _news(2), _news(3)]
    shuffled = _reply({'items': [_item(3), _item(1), _item(2)]})
    with patch.object(llm.requests, 'post', return_value=shuffled):
        asyncio.run(llm._llm_enrich_batch(pack))
        for news in pack:
            asyncio.run(llm._llm_enrich(news))
    assert [n['_llm_text'].split('\n')[0] for n in pack] == [
        'Заголовок 1.', 'Заголовок 2.', 'Заголовок 3.']


# ---------- частичный и битый ответ не теряет новости ----------

def test_missing_items_fall_back_to_single_calls(llm):
    """Новость, которой не оказалось в ответе, обязана дойти обычным путём.

    Иначе экономия вызовов превращалась бы в молча пропавшие новости.
    """
    pack = [_news(i) for i in range(1, 5)]
    partial = _reply({'items': [_item(1), _item(2)]})
    with patch.object(llm.requests, 'post', return_value=partial) as post:
        assert asyncio.run(llm._llm_enrich_batch(pack)) == 2
        post.reset_mock()
        post.return_value = _reply(_item(9))
        results = [asyncio.run(llm._llm_enrich(news)) for news in pack]
    assert results == ['ok'] * 4
    assert post.call_count == 2, 'неразобранные новости остались без модели'


def test_truncated_answer_keeps_what_arrived(llm):
    """Обрыв по лимиту токенов не должен стоить всей пачки.

    Внешний объект остаётся незакрытым, и разбор целиком возвращает None —
    хотя первые новости в ответе закрыты полностью и вполне пригодны.
    """
    full = json.dumps({'items': [_item(1), _item(2), _item(3)]}, ensure_ascii=False)
    cut = full[:full.index('Заголовок 3') + 5]      # оборвались на полуслове
    parsed = llm._llm_parse_batch(cut)
    assert llm._llm_parse_json(cut) is None, 'ответ перестал быть оборванным'
    assert set(parsed) == {1, 2}
    assert parsed[2]['title'] == 'Заголовок 2'


def test_garbage_answer_costs_one_call_and_loses_nothing(llm):
    """Мусор вместо JSON — это ноль разобранных, а не испорченные посты."""
    with patch.object(llm.requests, 'post', return_value=_reply_text('привет!')):
        assert asyncio.run(llm._llm_enrich_batch([_news(1), _news(2)])) == 0


def test_empty_item_is_not_remembered(llm):
    """Пустышка вида {"id":2} не должна попасть в кеш.

    Запомнить её — значит навсегда лишить новость модели: кеш будет отвечать
    на все следующие попытки, и пост выйдет без перевода и тегов.
    """
    pack = [_news(1), _news(2)]
    raw = json.dumps({'items': [_item(1), {'id': 2}]}, ensure_ascii=False)
    assert 2 not in llm._llm_parse_batch(raw), 'пустышка прошла как разбор'
    with patch.object(llm.requests, 'post', return_value=_reply_text(raw)):
        assert asyncio.run(llm._llm_enrich_batch(pack)) == 1
    assert llm._llm_editorial_cached(pack[1]) is None


def test_repeated_ids_do_not_overwrite_each_other(llm):
    """Два ответа с одним id — берём первый, а не последний.

    Иначе повтор в ответе модели мог бы подменить уже сопоставленный разбор.
    """
    parsed = llm._llm_parse_batch(json.dumps(
        {'items': [_item(1), dict(_item(1), title='Подмена')]}, ensure_ascii=False))
    assert parsed[1]['title'] == 'Заголовок 1'


def test_ids_outside_the_pack_are_ignored(llm):
    """Лишний id не должен приводить к разбору, которого никто не просил."""
    pack = [_news(1), _news(2)]
    answer = _reply({'items': [_item(1), _item(2), _item(7)]})
    with patch.object(llm.requests, 'post', return_value=answer):
        assert asyncio.run(llm._llm_enrich_batch(pack)) == 2


# ---------- кеш: за один и тот же ответ не платим дважды ----------

def test_cache_key_follows_the_content_not_the_object(llm):
    """Пост, вернувшийся из очереди, — уже другой dict, но та же новость."""
    assert llm._llm_content_key(_news(1)) == llm._llm_content_key(dict(_news(1)))
    assert llm._llm_content_key(_news(1)) != llm._llm_content_key(_news(2))


def test_cache_key_survives_reading_the_article(llm):
    """Ключ считается по ленте, а не по тексту статьи.

    Иначе один и тот же пост до похода за статьёй и после получал бы разные
    ключи, и повторная подготовка тратила бы второй вызов.
    """
    news = _news(1)
    before = llm._llm_content_key(news)
    news['_article_used'] = True
    news['_llm_topic'] = 'аниме'
    assert llm._llm_content_key(news) == before


def test_cached_answer_publishes_while_the_model_is_silent(llm, monkeypatch):
    """Ответ уже получен — ждать молчащую модель незачем.

    Без этого пост, подготовленный пачкой, откладывался бы вместе с
    провайдером, хотя весь нужный разбор давно лежит рядом.
    """
    news = _news(1)
    llm._llm_editorial_remember(news, _item(1))
    monkeypatch.setattr(llm, '_llm_disabled_runtime', True)
    monkeypatch.setattr(llm, '_llm_disabled_reason', 'circuit')
    with patch.object(llm.requests, 'post', return_value=_reply(_item(1))) as post:
        assert asyncio.run(llm._llm_enrich(news)) == 'ok'
    assert post.call_count == 0


def test_news_without_a_cached_answer_still_waits_for_the_model(llm, monkeypatch):
    """Кеш не должен отменять отсрочку: сырой пост остаётся в канале навсегда."""
    monkeypatch.setattr(llm, '_llm_deferred', {})
    monkeypatch.setattr(llm, '_llm_disabled_runtime', True)
    monkeypatch.setattr(llm, '_llm_disabled_reason', 'circuit')
    assert asyncio.run(llm._llm_enrich(_news(1))) == 'defer'


def test_connection_probe_ignores_the_cache(llm):
    """Диагностика обязана дойти до провайдера.

    Иначе /llm отчитался бы «модель жива» по ответу, полученному час назад, —
    ровно в тот момент, когда чинить нужно провайдера.
    """
    news = _news(1)
    llm._llm_editorial_remember(news, _item(1))
    with patch.object(llm.requests, 'post', return_value=_reply(_item(1))) as post:
        assert asyncio.run(llm._llm_enrich(dict(news), use_cache=False)) == 'ok'
    assert post.call_count == 1, 'проверка связи отвечает из кеша'


def test_stale_cache_is_not_reused(llm, monkeypatch):
    """Разбор недельной давности мог быть сделан другой моделью.

    Провайдер меняется на ходу (failover, /llmmodel), и вечный кеш тихо
    закреплял бы за новостью ответ, которого текущая модель не давала.
    """
    news = _news(1)
    llm._llm_editorial_remember(news, _item(1))
    monkeypatch.setattr(llm, 'LLM_EDITORIAL_CACHE_TTL_SEC', 0)
    assert llm._llm_editorial_cached(news) is None


def test_cache_is_bounded(llm):
    """Структура без потолка в долгоживущем процессе однажды выстреливает."""
    for i in range(llm.LLM_EDITORIAL_CACHE_MAX + 50):
        llm._llm_editorial_remember(_news(i), _item(1))
    assert len(llm._llm_editorial_cache) <= llm.LLM_EDITORIAL_CACHE_MAX


def test_prompt_version_invalidates_the_cache(llm, monkeypatch):
    """После смены промпта старые разборы сделаны по другим правилам."""
    news = _news(1)
    llm._llm_editorial_remember(news, _item(1))
    monkeypatch.setattr(llm, 'LLM_PROMPT_VERSION', 'editorial-next')
    assert llm._llm_editorial_cached(news) is None


# ---------- когда пачку собирать не надо ----------

def test_batching_can_be_switched_off(llm, monkeypatch):
    """Флаг обязан возвращать старое поведение без отката кода."""
    monkeypatch.setitem(llm.FEATURE_FLAGS, 'llm_batching', False)
    answer = _reply({'items': [_item(1), _item(2)]})
    with patch.object(llm.requests, 'post', return_value=answer) as post:
        assert asyncio.run(llm._llm_enrich_batch([_news(1), _news(2)])) == 0
    assert post.call_count == 0


def test_single_leftover_news_is_not_sent_as_a_pack(llm):
    """Одна новость стоит одного вызова в любом случае — пачка тут не нужна."""
    with patch.object(llm.requests, 'post', return_value=_reply(_item(1))) as post:
        assert asyncio.run(llm._llm_enrich_batch([_news(1)])) == 0
    assert post.call_count == 0


def test_silent_provider_stops_the_run(llm):
    """Пачка не ответила — следующая не ответит тоже.

    Попытки к бесплатному провайдеру не бесконечны: после серии ошибок
    открывается circuit, и упорство стоило бы модели на весь цикл.
    """
    pack = [_news(i) for i in range(1, 10)]
    with patch.object(llm.requests, 'post',
                      return_value=_reply_text('привет!')) as post:
        assert asyncio.run(llm._llm_enrich_batch(pack)) == 0
    assert post.call_count == 1, 'бот продолжает долбить молчащего провайдера'


def test_already_cached_news_is_not_sent_again(llm):
    """Пачка не должна нести то, за что уже заплачено."""
    pack = [_news(1), _news(2)]
    llm._llm_editorial_remember(pack[0], _item(1))
    llm._llm_editorial_remember(pack[1], _item(2))
    answer = _reply({'items': [_item(1), _item(2)]})
    with patch.object(llm.requests, 'post', return_value=answer) as post:
        assert asyncio.run(llm._llm_enrich_batch(pack)) == 0
    assert post.call_count == 0, 'пачка снова платит за уже полученные ответы'


def test_mirror_of_the_same_news_takes_one_slot(llm):
    """Один и тот же материал с двух источников — одна работа, а не две."""
    pack = [_news(1), dict(_news(1)), _news(2), _news(3)]
    answer = _reply({'items': [_item(1), _item(2), _item(3)]})
    with patch.object(llm.requests, 'post', return_value=answer) as post:
        assert asyncio.run(llm._llm_enrich_batch(pack)) == 3
    sent = post.call_args[1]['json']['messages'][1]['content']
    assert sent.count('<news id=') == 3, 'зеркало ленты заняло место в пачке'


def test_prefetch_skips_what_the_ledger_will_drop(llm, tmp_path, monkeypatch):
    """Дубль по похожему заголовку до модели не доходил и раньше.

    Место в пачке ограничено: отдать его новости, которую отсеет ledger,
    значит ослабить ровно ту экономию, ради которой пачка и нужна.
    """
    links = llm.SentLinksStore(tmp_path / 'sent.json')
    asyncio.run(links.claim('https://x.test/1', 'Bleach opening revealed'))
    monkeypatch.setattr(llm, 'sent_links', links)
    pack = [dict(_news(1), title='Bleach opening revealed'),
            dict(_news(2), title='Chainsaw Man movie gets a trailer'),
            dict(_news(3), title='Pokemon card sales get a hard limit')]
    answer = _reply({'items': [_item(1), _item(2)]})
    with patch.object(llm.requests, 'post', return_value=answer) as post:
        asyncio.run(llm._llm_prefetch_for_cycle(pack))
    sent = post.call_args[1]['json']['messages'][1]['content']
    assert 'Bleach opening revealed' not in sent
    assert sent.count('<news id=') == 2


# ---------- очередь канала ----------

def test_peek_does_not_touch_the_queue(llm, tmp_path):
    """Заглядывание вперёд не имеет права сдвинуть очередь.

    Иначе подготовка начала бы влиять на то, что и когда публикуется: пост мог
    бы уйти в inflight без отправки и не вернуться.
    """
    queue = llm.PostQueue(tmp_path / 'q.json')
    asyncio.run(queue.push_many([_queued(i) for i in range(1, 4)]))
    peeked = asyncio.run(queue.peek_next(2))
    assert [n['title'] for n in peeked] == ['Headline number 1', 'Headline number 2']
    assert asyncio.run(queue.peek_size()) == 3
    assert asyncio.run(queue.has_inflight()) is False
    assert asyncio.run(queue.pop_next())['title'] == 'Headline number 1'


def test_queue_head_is_prepared_by_the_pack(llm, tmp_path, monkeypatch):
    """Режим канала публикует по одному посту — и платил вызов за каждый."""
    queue = llm.PostQueue(tmp_path / 'q.json')
    asyncio.run(queue.push_many([_queued(i) for i in range(1, 5)]))
    monkeypatch.setattr(llm, 'post_queue', queue)
    answer = _reply({'items': [_item(i) for i in range(1, 5)]})
    with patch.object(llm.requests, 'post', return_value=answer) as post:
        assert asyncio.run(llm._llm_prefetch_queue_head()) == 4
        # Следующий тик: голова очереди уже разобрана, просить нечего.
        assert asyncio.run(llm._llm_prefetch_queue_head()) == 0
    assert post.call_count == 1


def test_prefetch_waits_until_the_head_needs_the_model(llm, tmp_path, monkeypatch):
    """Иначе пачка выродилась бы в вечный «один вызов на пост».

    На каждом тике мы добирали бы в неё ровно одну новую новость, и экономии
    не осталось бы вовсе.
    """
    queue = llm.PostQueue(tmp_path / 'q.json')
    asyncio.run(queue.push_many([_queued(i) for i in range(1, 4)]))
    monkeypatch.setattr(llm, 'post_queue', queue)
    head = asyncio.run(queue.peek_next(1))[0]
    llm._llm_editorial_remember(head, _item(1))
    answer = _reply({'items': [_item(i) for i in range(1, 4)]})
    with patch.object(llm.requests, 'post', return_value=answer) as post:
        assert asyncio.run(llm._llm_prefetch_queue_head()) == 0
    assert post.call_count == 0, 'пачка собирается на каждом тике заново'


def test_publishing_survives_a_broken_prefetch(llm, tmp_path, monkeypatch):
    """Подготовка — оптимизация, а не условие публикации.

    Её падение раньше просто не существовало; теперь оно не должно
    останавливать доставку — без кеша посты пойдут по одному, как и раньше.
    """
    queue = llm.PostQueue(tmp_path / 'q.json')
    asyncio.run(queue.push_many([_queued(1)]))
    monkeypatch.setattr(llm, 'post_queue', queue)

    async def boom():
        raise RuntimeError('провайдер сломался неожиданно')

    monkeypatch.setattr(llm, '_llm_prefetch_queue_head', boom)

    async def fake_send(_bot, news):
        return 'sent'

    monkeypatch.setattr(llm, 'send_news', fake_send)
    result, post = asyncio.run(llm._publish_one_from_queue(MagicMock()))
    assert result == 'sent'
    assert post['title'] == 'Headline number 1'


# ---------- метка принадлежности: разбор не должен уехать к чужой новости ----

def _marked(idx, src_idx=None):
    """Разбор с меткой ``src`` — первыми словами исходного заголовка."""
    return dict(_item(idx), src=f'Headline number {src_idx or idx}')


def test_swapped_analysis_does_not_reach_the_news(llm):
    """Разбор, уехавший к чужому id, обязан быть отброшен.

    Числа и даты проверяются дальше по конвейеру, а topic, kind и subject —
    нет: пост ушёл бы с чужой темой и чужим предметом дедупа, и заметить это
    было бы нечем. Метка «src» — единственное, по чему подмена видна.
    """
    pack = [_news(1), _news(2)]
    swapped = _reply({'items': [_marked(1, src_idx=2), _marked(2, src_idx=1)]})
    with patch.object(llm.requests, 'post', return_value=swapped) as post:
        assert asyncio.run(llm._llm_enrich_batch(pack)) == 0
        post.reset_mock()
        post.return_value = _reply(_item(9))
        assert [asyncio.run(llm._llm_enrich(news)) for news in pack] == ['ok', 'ok']
    assert post.call_count == 2, 'отброшенные новости остались без модели'


def test_correct_marker_lets_the_analysis_through(llm):
    """Проверка не должна заворачивать верные разборы — иначе экономии нет."""
    pack = [_news(1), _news(2)]
    answer = _reply({'items': [_marked(1), _marked(2)]})
    with patch.object(llm.requests, 'post', return_value=answer) as post:
        assert asyncio.run(llm._llm_enrich_batch(pack)) == 2
    assert post.call_count == 1


def test_marker_does_not_leak_into_the_post(llm):
    """«src» — служебная метка, а не часть разбора: в тексте её быть не должно."""
    news = _news(1)
    answer = _reply({'items': [_marked(1)]})
    with patch.object(llm.requests, 'post', return_value=answer):
        asyncio.run(llm._llm_enrich_batch([news, _news(2)]))
        assert asyncio.run(llm._llm_enrich(news)) == 'ok'
    assert 'src' not in (llm._llm_editorial_cached(news) or {})
    assert 'Headline' not in news['_llm_text']


def test_answer_without_a_marker_is_still_used(llm):
    """Модель вправе не вернуть метку — это не повод терять весь разбор."""
    pack = [_news(1), _news(2)]
    answer = _reply({'items': [_item(1), dict(_item(2), src='')]})
    with patch.object(llm.requests, 'post', return_value=answer):
        assert asyncio.run(llm._llm_enrich_batch(pack)) == 2


def test_marker_matching_several_news_equally_is_not_judged(llm):
    """Ничья означает «судить не по чему», а не «отбросить».

    Метка из слов, общих всей пачке, ничего не доказывает ни в одну сторону.
    Заворачивать по ней разборы — терять экономию на ровном месте.
    """
    assert llm._llm_batch_owner('Headline number', [_news(1), _news(2)]) == 0
    assert llm._llm_batch_owner('', [_news(1), _news(2)]) == 0
    assert llm._llm_batch_owner('нечто постороннее', [_news(1), _news(2)]) == 0
    # Ни одного общего слова с единственным заголовком — тоже не доказательство.
    assert llm._llm_batch_owner('нечто постороннее', [_news(1)]) == 0


def test_marker_picks_the_news_it_matches_best(llm):
    """Общие слова заголовков не должны мешать опознать нужную новость."""
    pack = [_news(1), _news(2), _news(3)]
    assert llm._llm_batch_owner('Headline number 3', pack) == 3
    assert llm._llm_batch_owner('Headline number 1', pack) == 1


def test_prompt_asks_for_the_marker(llm):
    """Без требования в промпте метка не придёт, и проверка станет пустой.

    Требований два, и нужны оба: поле в схеме ответа — чтобы модель вообще его
    вернула, и объяснение — чтобы вернула именно слова заголовка, а не пересказ.
    """
    prompt = llm.LLM_BATCH_SYSTEM_PROMPT
    assert '"src"' in prompt.split('Каждая новость обрабатывается')[0], (
        'поле «src» пропало из схемы ответа')
    assert prompt.count('src') >= 2, 'из промпта пропало объяснение метки'
