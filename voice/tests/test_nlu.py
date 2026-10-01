from __future__ import annotations

import pytest

from jarvisvoice.commands import ComboCmd, ItemCmd, LearnCmd, PressCmd, SpellCmd, StanceCmd, SystemCmd
from jarvisvoice.nlu import CommandParser, WakeWord, build_prompt, normalize_tokens


@pytest.fixture
def parser(config) -> CommandParser:
    return CommandParser(config)


def commands(parser: CommandParser, text: str, now: float = 0.0) -> list:
    return [parsed.command for parsed in parser.parse(text, now).commands]


def test_default_config_has_no_alias_conflicts(parser):
    assert parser.conflicts == []


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("Санстрайк!", ["санстрайк"]),
        ("Sunstrike", ["санстрайк"]),
        ("Sun Strike", ["сан", "страйк"]),
        ("Первая способность", ["первый", "способность"]),
        ("1-й слот предметов", ["первый", "слот", "предметов"]),
        ("Айс волл", ["айс", "вол"]),
        ("ЭМП", ["емп"]),
        ("Ёж", ["еж"]),
    ],
)
def test_normalize_tokens(text, tokens):
    assert normalize_tokens(text) == tokens


@pytest.mark.parametrize("text", ["санстрайк", "Sunstrike", "Сан страйк.", "санстраик"])
def test_spell_invoke_and_cast(parser, text):
    assert commands(parser, text) == [SpellCmd("sun_strike", label="Sun Strike")]


@pytest.mark.parametrize(
    ("text", "slot"),
    [
        ("Комбо 1.", 1),
        ("комбо один", 1),
        ("первое комбо", 1),
        ("Комбо-2", 2),
        ("комбо три", 3),
        ("Combo 5", 5),
        ("комбо шесть", 6),
        ("комбо 7", 7),
        ("комбо десять", 10),
        ("комбо одиннадцать", 11),
        ("Комбо 12", 12),
        ("комбо двенадцать", 12),
        ("комбо динамик", 0),
        ("комбо динамика", 0),
        ("динамическое комбо", 0),
    ],
)
def test_combo_by_number_and_dynamic(parser, text, slot):
    assert commands(parser, text) == [ComboCmd(slot, f"Комбо {slot}" if slot else "Комбо динамик")]


@pytest.mark.parametrize("text", ["комбо 13", "комбо 21", "комбо 0", "комбо", "комбо двадцать"])
def test_combo_number_must_be_exact(parser, text):
    assert commands(parser, text) == []


def test_combo_goes_along_with_other_commands(parser):
    assert commands(parser, "блинк комбо 3 бкб") == [
        ItemCmd(("item_blink", "item_overwhelming_blink", "item_swift_blink", "item_arcane_blink"), label="Blink"),
        ComboCmd(3, "Комбо 3"),
        ItemCmd(("item_black_king_bar",), label="BKB"),
    ]


def test_invoke_only_prefix_in_same_phrase(parser):
    (command,) = commands(parser, "скастуй метеор")
    assert command == SpellCmd("chaos_meteor", invoke_only=True, label="Chaos Meteor")


def test_invoke_only_prefix_as_separate_phrase(parser):
    result = parser.parse("скастуй", 10.0)
    assert result.commands == [] and result.waiting_for_spell
    (command,) = commands(parser, "метеор", 11.0)
    assert command.invoke_only
    # Префикс срабатывает один раз.
    (command,) = commands(parser, "метеор", 11.5)
    assert not command.invoke_only


def test_invoke_only_prefix_expires(parser, config):
    parser.parse("скастуй", 0.0)
    late = config.timing.invoke_only_window_ms / 1000 + 0.1
    (command,) = commands(parser, "метеор", late)
    assert not command.invoke_only


def test_noise_does_not_extend_invoke_only_window(parser, config):
    window = config.timing.invoke_only_window_ms / 1000
    parser.parse("скастуй", 0.0)
    parser.parse("эээ ну это", window - 0.5)
    (command,) = commands(parser, "метеор", window + 0.1)
    assert not command.invoke_only


def test_several_commands_in_one_phrase(parser):
    spells = [c.spell for c in commands(parser, "торнадо метеор бласт")]
    assert spells == ["tornado", "chaos_meteor", "deafening_blast"]


def test_longer_alias_wins(parser):
    (command,) = commands(parser, "алакрити на союзника")
    assert command.spell == "alacrity" and not command.self_cast
    (command,) = commands(parser, "алакрити")
    assert command.self_cast


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("первый скилл", PressCmd(bind="quas", label="первый скилл")),
        ("ульта", PressCmd(bind="invoke", label="ульта")),
        ("1 слот предметов", PressCmd(bind="item1", label="первый слот предметов")),
        ("шестой предмет", PressCmd(bind="item6", label="шестой слот предметов")),
        ("ТП", PressCmd(bind="tp", label="ТП")),
        ("холд", PressCmd(bind="hold", label="холд")),
        ("экзорт", StanceCmd("EEE", "сферы EEE")),
        ("бот мут", SystemCmd("mute", "мут")),
        ("выключи микрофон", SystemCmd("mute", "мут")),
        ("включись", SystemCmd("unmute", "снять мут")),
        ("отбой", SystemCmd("sleep", "сон")),
        ("выключись", SystemCmd("quit", "выход")),
    ],
)
def test_other_commands(parser, text, expected):
    assert commands(parser, text) == [expected]


def test_cold_and_hold_are_not_confused(parser):
    assert commands(parser, "колд")[0].spell == "cold_snap"
    assert commands(parser, "холд")[0].bind == "hold"


def test_learn(parser):
    assert commands(parser, "качай экзорт") == [LearnCmd("exort", "прокачка экзорт")]
    assert commands(parser, "прокачай первый") == [LearnCmd("quas", "прокачка квас")]


def test_items(parser):
    (bkb,) = commands(parser, "Б.К.Б.")
    assert isinstance(bkb, ItemCmd) and bkb.names == ("item_black_king_bar",) and not bkb.self_cast
    (euls,) = commands(parser, "еул на себя")
    assert euls.names == ("item_cyclone",) and euls.self_cast


def test_short_aliases_need_exact_match(parser):
    assert commands(parser, "тпш") == []
    assert commands(parser, "торт") == []


@pytest.mark.parametrize("text", ["емп", "ЭМП!", "EMP", "E.M.P.", "Е.М.П.", "эм пи", "и эм пи", "е эм пе"])
def test_emp_pronunciations(parser, text):
    assert commands(parser, text) == [SpellCmd("emp", label="EMP")]


def test_emp_in_combo_and_with_invoke_prefix(parser):
    assert [c.spell for c in commands(parser, "торнадо емп метеор")] == ["tornado", "emp", "chaos_meteor"]
    assert commands(parser, "скастуй емп") == [SpellCmd("emp", invoke_only=True, label="EMP")]


@pytest.mark.parametrize("text", ["темп", "кемп", "ем", "емпу"])
def test_similar_short_words_are_not_emp(parser, text):
    assert commands(parser, text) == []


@pytest.mark.parametrize("text", ["привет, как дела", "давай в лес", "ну и что", ""])
def test_regular_speech_is_not_a_command(parser, text):
    assert commands(parser, text) == []


def test_prompt_keeps_spells_and_fits(config):
    prompt = build_prompt(config)
    assert len(prompt) <= 420
    for word in ("санстрайк", "метеор", "алакрити", "бласт", "скастуй", "бкб"):
        assert word in prompt


def test_preview_does_not_consume_prefix_until_committed(parser):
    parser.parse("скастуй", 10.0)
    first = parser.parse("метеор", 11.0, commit=False)
    assert first.commands[0].command.invoke_only
    assert parser.parse("метеор", 11.0, commit=False).commands[0].command.invoke_only
    parser.commit(first)
    assert not parser.parse("метеор", 11.0).commands[0].command.invoke_only


@pytest.fixture(scope="module")
def wake(config):
    return WakeWord(config.wake.say, config.recognition.match_threshold)


@pytest.mark.parametrize(
    ("text", "rest"),
    [
        ("Джарвис, санстрайк метеор", "санстрайк метеор"),
        ("Jarvis, tornado", "торнадо"),
        ("Жарвис бкб", "бкб"),
        ("джар вис эмп", "емп"),  # Whisper разбил слово пополам; остаток уже нормализован
        ("ну короче джарвис стоп", "стоп"),
    ],
)
def test_wake_word_variants(wake, text, rest):
    assert wake.split(text, anywhere=False) == (True, rest)


@pytest.mark.parametrize("text", ["санстрайк метеор", "Дарвин", "джаз", "жара вист"])
def test_other_words_are_not_the_wake_word(wake, text):
    assert not wake.split(text, anywhere=False)[0]


def test_awake_bot_only_cuts_out_the_wake_word(wake):
    assert wake.split("тп джарвис метеор", anywhere=True) == (True, "тп метеор")
    assert wake.split("тп метеор", anywhere=True) == (False, "тп метеор")


def test_prompt_ends_with_wake_word(config):
    assert build_prompt(config).endswith("Джарвис.")


def labels(parser: CommandParser, text: str) -> list[str]:
    return [parsed.label for parsed in parser.parse(text, 0.0, commit=False).commands]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Одинаковый звук на стыке Whisper пишет один раз.
        ("А то санстрайк.", ["Atos", "Sun Strike"]),
        ("Блин, катаклизм, метеор", ["Blink", "Cataclysm", "Chaos Meteor"]),
        # Буква соседней команды приклеилась к слову.
        ("бласта, тос, хекс", ["Deafening Blast", "Atos", "Hex"]),
        ("хекса тос", ["Hex", "Atos"]),
        # Звучит так же, пишется иначе.
        ("пласт", ["Deafening Blast"]),
        ("тарнада", ["Tornado"]),
        ("Глеб, нир.", ["Atos"]),
        ("Догон.", ["Dagon"]),
        # Лог 19.09 22:50–22:54: быстрые комбо, склеенные Whisper'ом.
        (
            "Балинка тос, хекс, катаклизм, метеор, бласкл снэп.",
            ["Blink", "Atos", "Hex", "Cataclysm", "Chaos Meteor", "Deafening Blast", "Cold Snap"],
        ),
        ("Блин, катас, хекс, катаклизм", ["Blink", "Atos", "Hex", "Cataclysm"]),
        ("Блинк, католс, санстрайк", ["Blink", "Atos", "Sun Strike"]),  # «к» записана в оба слова
        ("санстрайк, метеор, бласков, снэп", ["Sun Strike", "Chaos Meteor", "Deafening Blast", "Cold Snap"]),
        ("А тос хекс, катаклизм, и тербласт.", ["Atos", "Hex", "Cataclysm", "Deafening Blast"]),
        ("катаклизм метеорбласт", ["Cataclysm", "Chaos Meteor", "Deafening Blast"]),
        # Лишняя гласная между согласными.
        ("Балинг, глипнир", ["Blink", "Atos"]),
        ("Торкл снэп", ["Cold Snap"]),  # короткий алиас («тор») внутри слова не ищется
    ],
)
def test_misheard_combos_from_the_log(parser, text, expected):
    assert labels(parser, text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "О, тут я не надо реп читать.",  # был «мут» по сходству с «бот мут»
        "ну короче сто пудов",  # «сто» + «п» не должны склеиться в «стоп»
        "Ой, блин.",
        "Ой, блядь.",
        "Весь проказ запорол.",
        "Бля, вырыв, начну читать.",
        "мана кончилась",
        # Отличаются от команды одним звуком, но не лишней гласной.
        "Ой, какой огонь!",  # «догон»
        "Сулка глупая не может дать.",  # «салька»
        "сколько можно",
        "Блин, ну тряди рэп, читайся, ебал.",
    ],
)
def test_everyday_speech_is_not_a_command(parser, text):
    assert labels(parser, text) == []


def test_ignored_word_right_before_a_command_is_that_command(parser):
    # В быстром комбо Whisper глотает «к»: «блин, хекс» — это Blink и Hex.
    assert labels(parser, "Блин, глипнир, хекс") == ["Blink", "Atos", "Hex"]
    assert labels(parser, "блин забыл") == []


def test_system_commands_need_exact_words(parser):
    assert labels(parser, "бот мут") == ["мут"]
    assert labels(parser, "замути") == []  # похоже на «замуть», но мут — только точно
    assert labels(parser, "отбой") == ["сон"]
    assert labels(parser, "отбоя") == []


def test_one_long_alias_beats_two_short_ones(parser):
    assert labels(parser, "колд снэп") == ["Cold Snap"]
    assert labels(parser, "колд колд") == ["Cold Snap", "Cold Snap"]
    assert labels(parser, "гост скипетр") == ["Ghost Scepter"]
    assert labels(parser, "тп санстрайк") == ["ТП", "Sun Strike"]


def test_extendable_commands_are_marked(parser):
    (alacrity,) = parser.parse("алакрити", 0.0).commands
    (sun_strike,) = parser.parse("санстрайк", 0.0).commands
    (ghost,) = parser.parse("гост", 0.0).commands
    assert alacrity.extendable and ghost.extendable and not sun_strike.extendable


@pytest.mark.parametrize(
    ("text", "ends"),
    [
        ("Блинк, катаклизм.", True),
        ("Джарвис, санстрайк", True),
        ("алакрити", True),
        ("Блинк, кат.", False),  # обрывок слова: продолжение может дать «катаклизм»
        ("хекс, блин", False),  # «блин» перед командой — Blink, но команды ещё нет
        ("метеор, скастуй", False),
        ("бля, ну", False),
        ("", False),
    ],
)
def test_phrase_ending_with_a_command(parser, text, ends):
    assert parser.parse(text, 0.0, commit=False).ends_with_command is ends


def test_long_combo_is_parsed_quickly(parser):
    import time

    text = ", ".join(["блинк санстрайк метеор колд снэп бласта тос хекс рефрешер"] * 3)
    started = time.perf_counter()
    assert len(parser.parse(text, 0.0).commands) == 24
    assert time.perf_counter() - started < 0.2
