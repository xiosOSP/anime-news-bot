"""Тесты автозащиты названий перед переводом."""
from anime_news_bot import auto_protect_proper_nouns


class TestQuotedTitles:
    def test_french_quotes(self):
        text = '«Detective Conan» episode 1200 announced'
        protected, ph = auto_protect_proper_nouns(text)
        assert 'Detective Conan' in ph.values()

    def test_ascii_quotes(self):
        text = 'Manga "Kanojo no Tomodachi" gets TV anime'
        protected, ph = auto_protect_proper_nouns(text)
        assert 'Kanojo no Tomodachi' in ph.values()

    def test_curly_quotes(self):
        text = '\u201CWitch Hat Atelier\u201D reveals new cast'
        protected, ph = auto_protect_proper_nouns(text)
        assert 'Witch Hat Atelier' in ph.values()


class TestUpperCase:
    def test_mappa_protected(self):
        text = 'MAPPA studio confirms remake'
        protected, ph = auto_protect_proper_nouns(text)
        assert 'MAPPA' in ph.values()

    def test_one_piece_protected(self):
        text = 'Wit Studio announces ONE PIECE remake'
        protected, ph = auto_protect_proper_nouns(text)
        assert 'ONE PIECE' in ph.values()

    def test_stopword_not_protected(self):
        # 'USA', 'TV', 'DVD' и подобные не должны защищаться
        text = 'New TV anime announced for USA'
        protected, ph = auto_protect_proper_nouns(text)
        # Не должно быть 'TV' или 'USA' среди защищённых
        for v in ph.values():
            assert v not in {'TV', 'USA', 'DVD'}

    def test_roman_numeral_not_protected_alone(self):
        # XIV в одиночку не должен защищаться — он должен идти с Final Fantasy
        text = 'Final Fantasy XIV expansion announced'
        protected, ph = auto_protect_proper_nouns(text)
        # XIV не должна быть отдельным placeholder'ом
        assert 'XIV' not in ph.values()


class TestJapaneseChains:
    def test_tonari_no_wakao(self):
        text = 'Tonari no Wakao-kun manga gets anime'
        protected, ph = auto_protect_proper_nouns(text)
        # Должно быть защищено как название (с no и -kun)
        values = ' '.join(ph.values())
        assert 'Wakao' in values
        assert 'Tonari no' in values or 'Tonari no Wakao' in values

    def test_tongari_boushi_no_atelier(self):
        text = 'Tongari Boushi no Atelier reveals new cast'
        protected, ph = auto_protect_proper_nouns(text)
        assert 'Tongari Boushi no Atelier' in ph.values()


class TestNoFalsePositives:
    def test_megathread_text_untouched(self):
        text = 'This is a daily megathread for general discussion'
        protected, ph = auto_protect_proper_nouns(text)
        # Не должно быть защиты обычных слов
        assert len(ph) == 0

    def test_common_english_phrases_safe(self):
        # Want, Don, Check, Have — обычные английские слова
        text = "Don't know what to start next? Check our wiki!"
        protected, ph = auto_protect_proper_nouns(text)
        for v in ph.values():
            assert v.lower() not in {'don', 'check', 'want', 'have', 'here'}

    def test_sentence_start_common_word(self):
        text = "The new chapter is great"
        protected, ph = auto_protect_proper_nouns(text)
        # The не должно вести цепочку
        for v in ph.values():
            assert not v.startswith('The ')


class TestExclamationTitles:
    def test_sound_euphonium(self):
        text = 'Sound! Euphonium new season announced'
        protected, ph = auto_protect_proper_nouns(text)
        # Должно ловить паттерн с !
        values = ' '.join(ph.values())
        assert 'Sound' in values or 'Euphonium' in values


class TestHyphenSuffix:
    def test_kun_suffix(self):
        # 'Wakao-kun' — слово с японским суффиксом, должно защититься
        text = 'New chapter of Wakao-kun released'
        protected, ph = auto_protect_proper_nouns(text)
        assert any('Wakao-kun' in v for v in ph.values())
