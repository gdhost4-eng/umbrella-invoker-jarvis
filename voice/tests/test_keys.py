from __future__ import annotations

import time

import pytest

from jarvisvoice import keys
from jarvisvoice.keys import BUTTONS, KeyHolder


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch):
    monkeypatch.setattr(keys, "_TAP_S", 0)
    monkeypatch.setattr(keys, "_GAP_S", 0)


def holder(linked: bool = True, foreground: bool = True, accepted: bool = True) -> tuple[KeyHolder, list]:
    sent: list[tuple[str, bool]] = []
    names = {button: name for name, button in BUTTONS.items()}

    def send(button, down):
        sent.append((names[button], down))
        return accepted

    return KeyHolder(lambda: linked, send, lambda: foreground), sent


def test_umbrella_button_names_map_to_windows_keys():
    assert BUTTONS["KEY_F13"].vk == 0x7C and BUTTONS["KEY_F24"].vk == 0x87
    assert BUTTONS["KEY_A"].vk == 0x41 and BUTTONS["KEY_0"].vk == 0x30 and BUTTONS["KEY_PAD_5"].vk == 0x65
    assert BUTTONS["KEY_DELETE"].extended and not BUTTONS["KEY_SPACE"].extended
    assert BUTTONS["KEY_MOUSE5"].mouse == 5 and not BUTTONS["KEY_MOUSE5"].vk
    assert "KEY_MWHEELUP" not in BUTTONS and "KEY_NONE" not in BUTTONS


def test_slot_key_is_tapped_and_combo_key_stays_down_until_released():
    held, sent = holder()
    held.run([{"op": "up"}, {"op": "tap", "keys": ["KEY_F14"]}, {"op": "down", "keys": ["KEY_MOUSE5"]}])
    assert sent == [("KEY_F14", True), ("KEY_F14", False), ("KEY_MOUSE5", True)]
    assert held.held == ("KEY_MOUSE5",)
    held.run([{"op": "up"}])
    assert sent[-1] == ("KEY_MOUSE5", False) and held.held == ()


def test_key_combination_is_released_in_reverse_order():
    held, sent = holder()
    held.run([{"op": "down", "keys": ["KEY_LALT", "KEY_Q"]}])
    held.release()
    assert sent == [("KEY_LALT", True), ("KEY_Q", True), ("KEY_Q", False), ("KEY_LALT", False)]


@pytest.mark.parametrize(
    "ops",
    [
        [{"op": "tap", "keys": ["KEY_F14"]}, {"op": "down", "keys": ["KEY_MWHEELUP"]}],
        [{"op": "down", "keys": "KEY_Q"}, {"op": "hold", "keys": ["KEY_Q"]}, "мусор"],
    ],
)
def test_unknown_keys_and_steps_press_nothing(ops):
    held, sent = holder()
    held.run(ops)
    assert sent == [] and held.held == ()


def test_nothing_is_pressed_into_another_window_but_release_still_works():
    held, sent = holder(foreground=False)
    held.run([{"op": "tap", "keys": ["KEY_F14"]}, {"op": "down", "keys": ["KEY_Q"]}])
    assert sent == []
    held.run([{"op": "up", "keys": ["KEY_Q"]}])
    assert sent == [("KEY_Q", False)]


def test_request_that_arrives_after_a_later_one_presses_nothing():
    held, sent = holder()
    held.run([{"op": "up"}], seq=5)  # отмена обогнала запрос, который она отменяет
    held.run([{"op": "tap", "keys": ["KEY_F14"]}, {"op": "down", "keys": ["KEY_Q"]}], seq=4)
    assert sent == [] and held.held == ()
    held.run([{"op": "down", "keys": ["KEY_Q"]}], seq=6)
    assert held.held == ("KEY_Q",)
    held.run([{"op": "reset"}])  # скрипт перезагрузили: клавиши отпущены, его счёт начинается с единицы
    held.run([{"op": "down", "keys": ["KEY_Q"]}], seq=1)
    assert sent == [("KEY_Q", True), ("KEY_Q", False), ("KEY_Q", True)]


def test_keys_are_released_when_the_script_stops_polling_and_on_exit(monkeypatch):
    monkeypatch.setattr(keys, "_LINK_CHECK_S", 0.01)
    held, sent = holder(linked=False)
    held.start()
    held.submit([{"op": "down", "keys": ["KEY_Q"]}])
    deadline = time.monotonic() + 2
    while sent != [("KEY_Q", True), ("KEY_Q", False)] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sent == [("KEY_Q", True), ("KEY_Q", False)]

    held, sent = holder()
    held.start()
    held.submit([{"op": "down", "keys": ["KEY_Q"]}])
    held.stop()
    assert sent == [("KEY_Q", True), ("KEY_Q", False)] and held.held == ()
