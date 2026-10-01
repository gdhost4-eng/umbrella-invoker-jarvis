from jarvisvoice.nlu import CommandParser
from jarvisvoice.streaming import StreamingCommands


def parse(config, text):
    return CommandParser(config).parse(text, 0.0, commit=False).commands


def labels(commands):
    return [parsed.label for parsed in commands]


def test_changed_partial_prefix_waits_for_the_final(config):
    stream = StreamingCommands()
    wrong = parse(config, "метеор емп")
    correct = parse(config, "торнадо емп")
    assert stream.partial(1, wrong) == wrong[:1]
    # Единственная выполненная команда пропала — гипотеза ненадёжна, ждём конец фразы.
    assert stream.partial(1, correct) == []
    finished = stream.finish(1, correct)
    assert labels(finished.pending) == ["Tornado", "EMP"]
    assert finished.skipped == []


def test_rewritten_start_confirmed_by_next_hypothesis_keeps_realtime(config):
    # Из лога 22:02: «Блинк, глипнир, хекс» стало «Балинг, глипнир, хекс…» — раньше до конца фразы
    # не выполнялось ничего, и хвост комбо ждал больше секунды. («Балинг» теперь и сам — Blink,
    # поэтому здесь слово, которое командой не считается.)
    stream = StreamingCommands()
    assert labels(stream.partial(1, parse(config, "блинк глипнир хекс"))) == ["Blink", "Atos"]
    assert stream.partial(1, parse(config, "баланс глипнир хекс атос")) == []
    assert labels(stream.partial(1, parse(config, "баланс глипнир хекс атос санстрайк"))) == ["Hex", "Atos"]
    finished = stream.finish(1, parse(config, "баланс глипнир хекс атос санстрайк метеор"))
    assert labels(finished.pending) == ["Sun Strike", "Chaos Meteor"]


def test_rewrite_that_changes_again_keeps_waiting(config):
    stream = StreamingCommands()
    stream.partial(1, parse(config, "блинк глипнир хекс"))
    assert stream.partial(1, parse(config, "баланс глипнир хекс атос")) == []
    assert stream.partial(1, parse(config, "торнадо хекс атос санстрайк")) == []


def test_command_fires_as_soon_as_the_next_word_starts(config):
    stream = StreamingCommands()
    parser = CommandParser(config)
    started = parser.parse("блинк, катакл", 0.0, commit=False)
    assert labels(stream.partial(1, started.commands, open_end=started.ends_with_command)) == ["Blink"]
    # «алакрити на со…» ещё может стать «алакрити на союзника» — ждём.
    extending = parser.parse("алакрити на со", 0.0, commit=False)
    assert stream.partial(2, extending.commands, open_end=extending.ends_with_command) == []


def test_repeated_command_is_preserved_without_replaying_prefix(config):
    stream = StreamingCommands()
    commands = parse(config, "емп емп метеор")
    assert stream.partial(1, commands) == commands[:2]
    assert stream.partial(1, commands) == []
    assert stream.finish(1, commands).pending == commands[2:]


def test_fuzzy_partial_fires_without_extra_wait(config):
    stream = StreamingCommands()
    commands = parse(config, "трнадо емп")
    assert commands[0].score < 100
    assert stream.partial(1, commands) == commands[:1]
    assert stream.partial(1, commands) == []
    assert stream.finish(1, commands).pending == commands[1:]


def test_lost_emitted_command_does_not_drop_the_rest(config):
    stream = StreamingCommands()
    commands = parse(config, "торнадо емп метеор")
    stream.partial(1, commands)
    assert stream.finish(1, commands[1:]).pending == commands[2:]
    assert stream.finish(2, commands).pending == commands


def test_final_that_lost_a_middle_command_keeps_the_combo(config):
    # Из лога: промежуточный результат слышал «бласт», итоговый — «глаз»; раньше остаток терялся.
    stream = StreamingCommands()
    partial = parse(config, "блинк санстрайк метеор бласт хекс атос")
    assert labels(stream.partial(1, partial)) == ["Blink", "Sun Strike", "Chaos Meteor", "Deafening Blast", "Hex"]
    final = parse(config, "блинк санстрайк метеор глаз хекс атос рефрешер")
    finished = stream.finish(1, final)
    assert labels(finished.pending) == ["Atos", "Refresher"]
    assert finished.skipped == []


def test_partial_that_lost_one_of_many_commands_keeps_going(config):
    stream = StreamingCommands()
    stream.partial(1, parse(config, "блинк санстрайк метеор бласт"))
    assert labels(stream.partial(1, parse(config, "блинк санстрайк глаз хекс атос"))) == ["Hex"]


def test_command_inserted_before_executed_ones_runs_late(config):
    # Из лога 22:53: промежуточная гипотеза не расслышала «хекс», итоговая — да; раньше Hex пропадал.
    stream = StreamingCommands()
    stream.partial(1, parse(config, "блинк санстрайк метеор"))
    finished = stream.finish(1, parse(config, "блинк атос санстрайк метеор бласт"))
    assert labels(finished.pending) == ["Atos", "Chaos Meteor", "Deafening Blast"]
    assert labels(finished.late) == ["Atos"]
    assert finished.skipped == []


def test_closing_partial_runs_inserted_command_once(config):
    stream = StreamingCommands()
    assert labels(stream.partial(1, parse(config, "блинк атос катаклизм метеор"))) == ["Blink", "Atos", "Cataclysm"]
    closing = parse(config, "блинк атос хекс катаклизм метеор")
    assert labels(stream.partial(1, closing, closing=True)) == ["Hex", "Chaos Meteor"]
    assert labels(stream.late) == ["Hex"]
    assert stream.finish(1, closing).pending == []


def test_command_that_replaced_an_executed_one_is_skipped(config):
    stream = StreamingCommands()
    stream.partial(1, parse(config, "блинк санстрайк метеор хекс"))
    finished = stream.finish(1, parse(config, "блинк атос санстрайк бласт"))
    # Метеор пропал, на его месте мог оказаться Атос — нажать оба было бы хуже.
    assert labels(finished.pending) == ["Deafening Blast"]
    assert labels(finished.skipped) == ["Atos"]


def test_closing_partial_fires_last_command_unless_it_can_be_extended(config):
    stream = StreamingCommands()
    assert labels(stream.partial(1, parse(config, "санстрайк"), closing=True)) == ["Sun Strike"]
    assert stream.finish(1, parse(config, "санстрайк")).pending == []
    assert stream.partial(2, parse(config, "алакрити"), closing=True) == []
    assert labels(stream.finish(2, parse(config, "алакрити на союзника")).pending) == ["Alacrity на союзника"]


def test_new_phrase_can_repeat_command_immediately(config):
    stream = StreamingCommands()
    commands = parse(config, "торнадо емп")
    stream.partial(1, commands)
    assert stream.partial(2, commands) == commands[:1]
    assert stream.finish(2, commands).pending == commands[1:]


def test_empty_hypothesis_does_not_cause_replay(config):
    stream = StreamingCommands()
    commands = parse(config, "торнадо емп")
    stream.partial(1, commands)
    stream.partial(1, [])
    assert stream.partial(1, commands) == []
    assert stream.finish(1, commands).pending == commands[1:]
