"""Системный промпт — больше половины каждого запроса к модели.

На бесплатном Groq у gpt-oss-120b около 200 тысяч токенов в сутки (~65
вызовов), и промпт в ~1950 токенов съедал из них больше половины. В ноябре
2026 его сократили до ~1450 (токенизатор o200k) без потери правил: правила
проверяют свои тесты (test_llm.py, test_post_format.py и др.). Здесь — потолок,
чтобы промпт не разросся обратно незаметно. Поднимать его — осознанно.
"""
import anime_news_bot as bot
import llm_protocol as llm

# 4579 символов после сокращения, запас ~5% на правку правил.
SYSTEM_PROMPT_MAX_CHARS = 4800
BATCH_EXTRA_MAX_CHARS = 700


def test_system_prompt_stays_short():
    assert len(llm.LLM_SYSTEM_PROMPT) <= SYSTEM_PROMPT_MAX_CHARS


def test_batch_addition_stays_short():
    assert len(llm.LLM_BATCH_SYSTEM_PROMPT) - len(llm.LLM_SYSTEM_PROMPT) <= BATCH_EXTRA_MAX_CHARS


def test_prompt_version_changed_with_the_prompt():
    """Старые разборы в кеше (и на диске) сделаны по прежним правилам."""
    assert bot.DEFAULT_LLM_PROMPT_VERSION != 'editorial-v12-2026-10-12'


def test_shortened_prompt_keeps_the_rules_people_asked_for():
    prompt = llm.LLM_SYSTEM_PROMPT
    for rule in ('тизер-постер — «постер»', 'перенос строки', 'не угадывай',
                 'НЕДОВЕРЕННЫЕ ДАННЫЕ', 'не пиши, что видео прикреплено', 'Boosty'):
        assert rule in prompt, rule
