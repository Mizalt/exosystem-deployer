"""Unit-тесты хелперов расширенного режима (app/run_config.py, Идея 2а)."""
import json

from app import run_config


def test_parse_env_from_dict():
    assert run_config.parse_env_input({"A": "1", "B": 2}) == {"A": "1", "B": "2"}


def test_parse_env_from_lines():
    text = "FOO=bar\n# комментарий\n\nBAZ = qux \nNOEQ\n=novalue"
    assert run_config.parse_env_input(text) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_empty():
    assert run_config.parse_env_input(None) == {}
    assert run_config.parse_env_input("") == {}
    assert run_config.parse_env_input("   ") == {}


def test_env_to_json_and_back():
    js = run_config.env_to_json("A=1\nB=2")
    assert json.loads(js) == {"A": "1", "B": "2"}
    assert run_config.env_from_json(js) == {"A": "1", "B": "2"}


def test_env_to_json_empty_is_none():
    assert run_config.env_to_json("") is None
    assert run_config.env_to_json({}) is None


def test_env_from_json_tolerant_to_garbage():
    assert run_config.env_from_json("not json") == {}
    assert run_config.env_from_json(None) == {}
    assert run_config.env_from_json("[1,2,3]") == {}  # не объект


def test_effective_port():
    assert run_config.effective_port(None) == 80  # старый деплой без колонки
    assert run_config.effective_port(8080) == 8080
    assert run_config.effective_port(0) == 0       # worker без сетевого порта сохраняется


def test_effective_port_prefers_explicit_then_detected_then_default():
    # Явный порт — приоритет над авто-детектом.
    assert run_config.effective_port(8080, 3000) == 8080
    # Явный 0 (worker) тоже приоритет — авто-детект не перебивает.
    assert run_config.effective_port(0, 3000) == 0
    # Порт не задан (None) → подхватываем detected (EXPOSE образа, напр. Next.js 3000).
    assert run_config.effective_port(None, 3000) == 3000
    # Ни явного, ни детекта → дефолт 80.
    assert run_config.effective_port(None, None) == 80
