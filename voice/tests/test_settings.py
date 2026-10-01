from jarvisvoice.settings import save_microphone, unique_devices


def test_save_microphone_keeps_other_settings(tmp_path):
    config = tmp_path / "config.yaml"
    local = tmp_path / "config.local.yaml"
    local.write_text("recognition:\n  device: cpu\n", encoding="utf-8")
    save_microphone(config, "HyperX")
    text = local.read_text(encoding="utf-8")
    assert "device: HyperX" in text and "device: cpu" in text


def test_save_microphone_default_removes_key(tmp_path):
    config = tmp_path / "config.yaml"
    save_microphone(config, "HyperX")
    save_microphone(config, None)
    assert "HyperX" not in (tmp_path / "config.local.yaml").read_text(encoding="utf-8")


def test_unique_devices_merges_host_apis():
    devices = [{"name": "HyperX Mic"}, {"name": "hyperx mic "}, {"name": "Realtek"}]
    assert unique_devices(devices) == ["HyperX Mic", "Realtek"]
