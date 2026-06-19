"""Тесты настроек панели (домен/SSL): загрузка и сохранение."""
from pathlib import Path

from app import panel_config


def test_load_missing_returns_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(panel_config, "CONFIG_FILE", tmp_path / "nope.json")
    s = panel_config.load_settings()
    assert s.domain is None
    assert s.ssl_cert_name is None


def test_save_then_load_roundtrip(monkeypatch, tmp_path):
    cfg = tmp_path / "panel.json"
    monkeypatch.setattr(panel_config, "CONFIG_FILE", cfg)

    panel_config.save_settings(
        panel_config.PanelSettings(domain="app.example.com", ssl_cert_name="app.example.com")
    )
    s = panel_config.load_settings()

    assert isinstance(cfg, Path)
    assert s.domain == "app.example.com"
    assert s.ssl_cert_name == "app.example.com"


def test_load_corrupt_json_returns_defaults(monkeypatch, tmp_path):
    cfg = tmp_path / "broken.json"
    cfg.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(panel_config, "CONFIG_FILE", cfg)
    s = panel_config.load_settings()
    assert s.domain is None
