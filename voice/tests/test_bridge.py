from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from jarvisvoice.bridge import Bridge
from jarvisvoice.commands import ComboCmd, ItemCmd, LearnCmd, PressCmd, SpellCmd, StanceCmd, SystemCmd, to_wire


def poll(bridge: Bridge, **request) -> dict:
    request.setdefault("session", bridge.session)
    request.setdefault("wait_ms", 0)
    return bridge.poll(request)


def test_commands_are_delivered_once_in_order_with_their_age():
    bridge = Bridge(0)
    now = time.monotonic()
    bridge.push(to_wire(SpellCmd("sun_strike")), "Sun Strike", now - 0.2, now - 0.3)
    bridge.push(to_wire(ItemCmd(("item_blink",), label="Blink")), "Blink", now)
    reply = poll(bridge, after=0)
    assert [c["seq"] for c in reply["commands"]] == [1, 2]
    first = reply["commands"][0]
    assert (first["kind"], first["spell"], first["label"]) == ("spell", "sun_strike", "Sun Strike")
    assert 150 <= first["age_ms"] <= 1000 and first["speech_age_ms"] >= first["age_ms"]
    assert "speech_age_ms" not in reply["commands"][1]
    assert poll(bridge, after=reply["seq"])["commands"] == []


def test_unknown_session_starts_from_current_position():
    bridge = Bridge(0)
    bridge.push({"kind": "stance", "orbs": "EEE"}, "экзорт", time.monotonic())
    reply = bridge.poll({"after": 0, "wait_ms": 0})  # скрипт перезагрузили: старые команды не повторяются
    assert reply["commands"] == [] and reply["seq"] == 1 and reply["session"] == bridge.session


def test_stale_commands_are_not_delivered():
    bridge = Bridge(0)
    bridge.push({"kind": "press", "bind": "tp", "double": False}, "ТП", time.monotonic() - 60)
    assert poll(bridge, after=0)["commands"] == []


def test_long_poll_returns_as_soon_as_a_command_arrives():
    bridge = Bridge(0)
    state_v = poll(bridge)["state_v"]
    timer = threading.Timer(0.1, lambda: bridge.push({"kind": "cancel"}, "отмена", time.monotonic()))
    timer.start()
    started = time.monotonic()
    reply = poll(bridge, after=0, state_v=state_v, wait_ms=2000)
    timer.join()
    assert [c["kind"] for c in reply["commands"]] == ["cancel"]
    assert time.monotonic() - started < 1.0


def test_state_change_wakes_the_poll_and_level_only_matters_while_listening():
    bridge = Bridge(0)
    bridge.set_state("sleeping", wake="Джарвис")
    state_v = poll(bridge)["state_v"]
    bridge.set_level(0.9)
    started = time.monotonic()
    assert poll(bridge, state_v=state_v, wait_ms=200)["state_v"] == state_v  # спит: громкость не будит запрос
    assert time.monotonic() - started >= 0.15

    bridge.set_state("listening", wake="Джарвис")
    reply = poll(bridge, state_v=state_v, wait_ms=2000)
    assert reply["state"]["activity"] == "listening" and reply["state"]["level"] == 0.9
    bridge.set_level(0.2)
    started = time.monotonic()
    assert poll(bridge, state_v=reply["state_v"], wait_ms=2000)["state"]["level"] == 0.2
    assert time.monotonic() - started < 1.0


def test_results_and_actions_reach_the_bot():
    results, actions = [], []
    bridge = Bridge(0, results.extend, actions.extend)
    poll(bridge, results=[{"label": "BKB", "ok": False, "message": "нет предмета"}, "мусор"], actions=["wake", "rm"])
    assert results == [{"label": "BKB", "ok": False, "message": "нет предмета"}]
    assert actions == ["wake"]
    assert bridge.connected


def test_key_requests_reach_the_bot_and_a_reloaded_script_releases_them():
    keys = []
    bridge = Bridge(0, on_keys=lambda steps, seq: keys.append((steps, seq)))
    steps = [{"op": "tap", "keys": ["KEY_F14"]}, {"op": "down", "keys": ["KEY_MOUSE5"]}]
    # отдельный запрос без session, как у actions
    bridge.poll({"keys": [*steps, "мусор"], "keys_seq": 7, "wait_ms": 0})
    assert keys == [(steps, 7)]
    poll(bridge)
    assert keys == [(steps, 7)]
    bridge.poll({"session": "", "after": 0, "wait_ms": 0})  # скрипт перезагрузили, пока клавиша зажата
    assert keys == [(steps, 7), ([{"op": "reset"}], None)]


@pytest.mark.parametrize(
    "command,wire",
    [
        (
            SpellCmd("tornado", invoke_only=True),
            {"kind": "spell", "spell": "tornado", "self_cast": False, "invoke_only": True},
        ),
        (PressCmd("tp", double=True), {"kind": "press", "bind": "tp", "double": True}),
        (ItemCmd(("item_cyclone",), True), {"kind": "item", "names": ["item_cyclone"], "self_cast": True}),
        (StanceCmd("EEE"), {"kind": "stance", "orbs": "EEE"}),
        (LearnCmd("exort"), {"kind": "learn", "bind": "exort"}),
        (ComboCmd(3), {"kind": "combo", "slot": 3}),
        (ComboCmd(0), {"kind": "combo", "slot": 0}),
    ],
)
def test_wire_format(command, wire):
    assert to_wire(command) == wire


def test_system_commands_never_go_to_the_game():
    with pytest.raises(ValueError):
        to_wire(SystemCmd("quit"))


def test_http_round_trip_post_and_get():
    actions = []
    bridge = Bridge(0, on_actions=actions.extend)
    bridge.start()
    try:
        port = bridge._httpd.server_address[1]
        bridge.push({"kind": "press", "bind": "stop", "double": False}, "стоп", time.monotonic())
        body = json.dumps({"session": bridge.session, "after": 0, "wait_ms": 0, "actions": ["mute"]}).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{port}/poll", data=body, method="POST")
        with urllib.request.urlopen(request, timeout=3) as response:
            reply = json.loads(response.read().decode("utf-8"))
        assert reply["commands"][0]["label"] == "стоп" and actions == ["mute"]

        url = f"http://127.0.0.1:{port}/poll?session={bridge.session}&after=1&wait_ms=0&actions=wake,sleep"
        with urllib.request.urlopen(url, timeout=3) as response:
            reply = json.loads(response.read().decode("utf-8"))
        assert reply["commands"] == [] and actions == ["mute", "wake", "sleep"]
    finally:
        bridge.stop()
