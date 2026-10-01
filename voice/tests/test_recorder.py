import numpy as np

from jarvisvoice.audio import load_wav
from jarvisvoice.recorder import PhraseRecorder


def test_keeps_only_the_latest_phrases(tmp_path):
    recorder = PhraseRecorder(tmp_path / "audio", limit=2)
    names = [recorder.save(np.full(1600, 0.1 * i, dtype=np.float32), i) for i in range(1, 4)]
    assert all(names)
    assert sorted(p.name for p in (tmp_path / "audio").glob("*.wav")) == sorted(names[1:])
    audio = load_wav(tmp_path / "audio" / names[-1])
    assert len(audio) == 1600 and abs(float(audio[0]) - 0.3) < 1e-3


def test_disabled_recorder_writes_nothing(tmp_path):
    recorder = PhraseRecorder(tmp_path / "audio", limit=0)
    assert recorder.save(np.ones(1600, dtype=np.float32), 1) is None
    assert not (tmp_path / "audio").exists()
