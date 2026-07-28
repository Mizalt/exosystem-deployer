"""ЯДРО: гейт показа ИИ-помощника панели (ADR-103).

`GET /api/panel/ai-availability` абстрагирует ИСТОЧНИК ИИ для фронта панели: нода
ключа/провайдера НЕ касается и ИИ не проксирует. Доступен только когда панель
открыта ВНУТРИ ЛК (embedded — есть pushed `embed_origin` контрол-плейна, ADR-092);
standalone → available:false → виджета нет вовсе. Плюс: capability `panel_ai`
объявлена (ЛК гейтит фичу по ней); паритет whitelist страниц панели фронт↔бэк.
"""
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_embed(monkeypatch, tmp_path):
    """Изоляция embed-настройки: env снят, файл-конфиг во временную папку."""
    from app import embed_config
    monkeypatch.delenv("DEPLOYER_EMBED_ORIGIN", raising=False)
    monkeypatch.setattr(embed_config, "CONFIG_FILE", tmp_path / "embed_origin.json")
    embed_config._cache = {"mtime": None, "value": None}
    yield


# --------------------------------------------------------------------------- #
#  Эндпоинт ноды: embedded → cloud, standalone → false
# --------------------------------------------------------------------------- #

def test_availability_false_when_standalone(auth_client):
    """Нет pushed embed_origin (standalone-панель) → available:false, виджета нет."""
    client, _ = auth_client
    r = client.get("/api/panel/ai-availability")
    assert r.status_code == 200
    body = r.json()
    assert body == {"available": False, "mode": None, "ai_origin": None}


def test_availability_cloud_when_embedded(auth_client, monkeypatch):
    """Панель embedded (есть pushed embed_origin ЛК) → available:true, mode:cloud,
    ai_origin = origin ЛК (куда фронт шлёт вопрос)."""
    from app import embed_config
    client, _ = auth_client
    embed_config.save_origin("https://lk.exosystem.tech")
    r = client.get("/api/panel/ai-availability")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["mode"] == "cloud"
    assert body["ai_origin"] == "https://lk.exosystem.tech"


def test_availability_from_env_origin(auth_client, monkeypatch):
    """env DEPLOYER_EMBED_ORIGIN (self-host руками) тоже включает виджет."""
    from app import embed_config
    monkeypatch.setenv("DEPLOYER_EMBED_ORIGIN", "https://panel.lk.example")
    embed_config._cache = {"mtime": None, "value": None}
    client, _ = auth_client
    r = client.get("/api/panel/ai-availability")
    assert r.json()["ai_origin"] == "https://panel.lk.example"


def test_availability_requires_auth(api_env):
    """Гейт под логином панели (как остальные /api/panel/*)."""
    _, _, client = api_env
    assert client.get("/api/panel/ai-availability").status_code == 401


# --------------------------------------------------------------------------- #
#  Capability panel_ai (ЛК гейтит фичу по ней)
# --------------------------------------------------------------------------- #

def test_capability_panel_ai_present():
    from app import version
    assert version.supports("panel_ai") is True
    assert "panel_ai" in version.describe()["capabilities"]
    # Аддитивно: существующие capability не потеряны.
    for cap in ("version", "panel_embed", "pro_license"):
        assert version.supports(cap) is True


def test_capabilities_sorted_no_dups():
    """Список детерминирован (сортирован, без дублей) — не сломали version/update_info."""
    from app import version
    caps = version.describe()["capabilities"]
    assert caps == sorted(set(caps))


# --------------------------------------------------------------------------- #
#  Паритет whitelist страниц панели: фронт (panel_ai.js) ↔ бэк (panel_knowledge)
# --------------------------------------------------------------------------- #

def test_nav_targets_front_back_parity():
    """Зеркальный whitelist nav-ключей: JS-виджет панели и knowledge ЛК ведут по ОДНИМ
    страницам (иначе кнопка «Перейти» ведёт в никуда). ADR-123/125: виджет теперь
    держит ДВА зеркала — NAV_PANEL (hash-навигация панели) и NAV_LK (кросс-хост в ЛК)."""
    from app.cloud.services import assistant_knowledge, panel_knowledge
    js = (_ROOT / "static" / "js" / "panel_ai.js").read_text(encoding="utf-8")

    def front_keys(var):
        m = re.search(var + r"\s*=\s*\{(.+?)\};", js, re.S)
        assert m, f"{var} не найден в panel_ai.js"
        return set(re.findall(r"(\w+):\s*'", m.group(1)))

    # Панельное зеркало == панельный whitelist бэка (исторический контракт).
    assert front_keys("var NAV_PANEL") == set(panel_knowledge.NAV_TARGETS), (
        "расхождение NAV_PANEL фронт↔бэк")
    # ЛК-зеркало (кросс-хост проводник) == whitelist разделов ЛК бэка.
    assert front_keys("var NAV_LK") == set(assistant_knowledge.NAV_TARGETS_LK), (
        "расхождение NAV_LK фронт↔бэк")


def test_widget_no_raw_innerhtml_of_model_answer():
    """Анти-XSS инвариант (ADR-091): ответ модели рендерится ПОВЕРХ escape-текста —
    в mdLite вход сначала прогоняется через esc(), сырого innerHTML модели нет."""
    js = (_ROOT / "static" / "js" / "panel_ai.js").read_text(encoding="utf-8")
    # mdLite экранирует вход первым делом.
    assert re.search(r"function mdLite\(text\)\s*\{\s*var lines = esc\(text\)", js), (
        "mdLite должен экранировать вход перед разметкой")
    # Вопрос пользователя — только через textContent (никогда innerHTML).
    assert "el.textContent = content" in js
