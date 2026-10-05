from __future__ import annotations

from workplane import config


def test_config_is_read_from_the_xdg_config_home(tmp_path, monkeypatch):
    (tmp_path / "workplane").mkdir()
    (tmp_path / "workplane" / "config.toml").write_text('me = ["carol"]\n')
    monkeypatch.delenv("WORKPLANE_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.load().me == ("carol",)


def test_database_lives_under_the_xdg_data_home_unless_overridden(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKPLANE_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert config.load(tmp_path / "none.toml").database_path == tmp_path / "workplane" / "workplane.db"

    monkeypatch.setenv("WORKPLANE_DB", str(tmp_path / "other.db"))
    assert config.load(tmp_path / "none.toml").database_path == tmp_path / "other.db"
