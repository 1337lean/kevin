from __future__ import annotations

from kevin.cogs import music


def test_load_opus_uses_homebrew_prefix(monkeypatch, tmp_path) -> None:
    prefix = tmp_path / "homebrew"
    ffmpeg = prefix / "bin" / "ffmpeg"
    opus = prefix / "lib" / "libopus.dylib"
    ffmpeg.parent.mkdir(parents=True)
    opus.parent.mkdir(parents=True)
    ffmpeg.touch()
    opus.touch()
    loaded: list[str] = []

    monkeypatch.delenv("OPUS_LIBRARY", raising=False)
    monkeypatch.setattr(music.discord.opus, "is_loaded", lambda: False)
    monkeypatch.setattr(music.discord.opus, "load_opus", loaded.append)
    monkeypatch.setattr(music.ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(music.shutil, "which", lambda _name: str(ffmpeg))

    assert music.load_opus_library()
    assert loaded == [str(opus)]


def test_load_opus_returns_false_when_no_candidate_loads(monkeypatch) -> None:
    monkeypatch.setenv("OPUS_LIBRARY", "missing-opus")
    monkeypatch.setattr(music.discord.opus, "is_loaded", lambda: False)
    monkeypatch.setattr(
        music.discord.opus,
        "load_opus",
        lambda _candidate: (_ for _ in ()).throw(OSError("not found")),
    )
    monkeypatch.setattr(music.ctypes.util, "find_library", lambda _name: None)
    monkeypatch.setattr(music.shutil, "which", lambda _name: None)

    assert not music.load_opus_library()
