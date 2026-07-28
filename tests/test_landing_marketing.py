"""Проверки маркетингового лендинга marketing/landing/index.html.

Самодостаточный статический лендинг (раздаётся как static, без бэкенда).
Тесты не импортируют приложение — только читают файлы и валидируют разметку,
поэтому не зависят от cloud-редакции и запускаются в любом окружении.

Пинуем зонтичную историю (деплоер + готовые решения + ИИ-студия + лендинги
в одну CRM), отсутствие BOM, базовый баланс тегов и целостность локальных
ассетов и legal-ссылок.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

LANDING_DIR = Path(__file__).resolve().parents[1] / "marketing" / "landing"
INDEX = LANDING_DIR / "index.html"
CSS = LANDING_DIR / "assets" / "css" / "style.css"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_index_exists():
    assert INDEX.is_file(), f"нет файла лендинга: {INDEX}"


def test_no_bom():
    # BOM ломает самодостаточную отдачу как static; должен отсутствовать.
    for path in (INDEX, CSS):
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"BOM в {path.name}"


def test_umbrella_hero_story_present(html: str):
    # Зонтичный лид: одна платформа — четыре дорожки (деплой своего кода,
    # готовые блоки, ИИ-студия, лендинги → одна CRM) на сервере клиента.
    for needle in (
        "Система для вашего дела",     # заголовок hero
        "вашем сервере",               # self-hosted — ядро позиционирования
        "готовых блоков",              # композиция из компонентов в hero-sub
        "Начать бесплатно",            # CTA hero
        'id="paths"',                  # секция «Что вы можете» (4 дорожки)
        'id="deploy"',                 # дорожка: деплоер first-class
        'id="components"',             # дорожка: витрина готовых решений
        'id="studio"',                 # дорожка: ИИ-студия
        'id="leadgen"',                # дорожка: лендинги → одна CRM
    ):
        assert needle in html, f"нет ключевого блока новой истории: {needle!r}"


def test_component_showcase_cards(html: str):
    # Витрина-тизер реальных компонентов + посыл «набор растёт вместе с делом».
    for slug in ("crm-leads", "landing-capture", "auth-cabinet"):
        assert slug in html, f"нет карточки компонента: {slug}"
    assert "по мере роста" in html, "утерян посыл «включайте модули по мере роста»"


def _comp_card_classes(html: str, slug: str) -> str:
    """Классы карточки компонента, в чьём <span class="slug"> лежит slug."""
    pos = html.index(f">{slug}<")
    start = html.rindex('<div class="comp ', 0, pos)
    return html[start : html.index(">", start)]


def test_unreleased_components_marked_soon(html: str):
    # Витрина не должна выдавать неготовые блоки за готовые: booking/blog/
    # payments-robokassa не выпущены как компоненты маркета и обязаны нести
    # бейдж «Скоро».
    assert "Скоро" in html, "нет бейджа «Скоро» для компонентов в разработке"
    assert html.count("comp-soon") >= 3, "не все компоненты в разработке помечены"
    for slug in ("booking", "blog", "payments-robokassa"):
        assert "soon" in _comp_card_classes(html, slug), f"{slug} не помечен «Скоро»"
    # Реальные компоненты «Скоро» помечаться не должны.
    for slug in ("crm-leads", "landing-capture", "auth-cabinet"):
        assert "soon" not in _comp_card_classes(html, slug), f"{slug} ошибочно «Скоро»"


def test_payments_provider_is_robokassa(html: str):
    # Платёжный провайдер — Робокасса; следов ЮKassa быть не должно.
    assert "Робокасс" in html, "нет упоминания Робокассы в платёжном блоке"
    low = html.lower()
    for bad in ("yookassa", "юkassa", "юкасс"):
        assert bad not in low, f"на лендинге остался след ЮKassa: {bad}"


def test_b2b_scenario_flow(html: str):
    # Конкретный B2B-пример: посадка → CRM → твой сервер.
    assert 'class="scenario' in html
    assert "Лендинг-посадка" in html
    assert "CRM с воронкой" in html


def test_self_host_pillars_preserved(html: str):
    # Сильные стороны старого позиционирования сохранены под новой рамкой.
    for needle in ("BYOA", "Данные в РФ", "HTTPS", "Начните бесплатно"):
        assert needle in html, f"утерян столп: {needle!r}"


def test_single_pro_tier(html: str):
    # Тарифы упрощены до Free + один Pro с ИИ (без сложной матрицы).
    assert ">Free<" in html
    assert ">Pro<" in html
    assert ">Business<" not in html, "тариф Business должен быть убран (один Pro)"


def test_legal_links_intact(html: str):
    for path in ("/legal/terms", "/legal/privacy", "/legal/consent"):
        assert f"https://lk.exosystem.tech{path}" in html, f"битая legal-ссылка: {path}"


def test_lk_entry_link_present(html: str):
    assert "https://lk.exosystem.tech" in html


def test_no_external_cdn(html: str):
    # Самодостаточность: только локальные ассеты, без внешних стилей/скриптов.
    assert 'href="assets/css/style.css"' in html
    assert 'src="assets/js/main.js"' in html
    for bad in ("cdn.", "googleapis", "unpkg", "jsdelivr", "cdnjs"):
        assert bad not in html, f"внешний CDN на лендинге: {bad}"


def test_local_assets_exist(html: str):
    # Все локальные ссылки на assets/... указывают на реально существующие файлы.
    refs = set(re.findall(r'(?:href|src)="(assets/[^"]+)"', html))
    assert refs, "не найдено ни одной ссылки на локальные ассеты"
    for ref in refs:
        assert (LANDING_DIR / ref).is_file(), f"битый локальный ассет: {ref}"


def test_tag_balance(html: str):
    # Грубый баланс парных тегов основных контейнеров (защита от обрыва секции).
    void = {
        "meta", "link", "img", "br", "hr", "input", "source",
        "rect", "path", "circle", "polyline", "line", "use", "stop",
    }
    counts: dict[str, int] = {}
    for m in re.finditer(r"<\s*(/?)\s*([a-zA-Z0-9]+)", html):
        closing, name = m.group(1), m.group(2).lower()
        if name in void:
            continue
        counts[name] = counts.get(name, 0) + (-1 if closing else 1)
    for tag in ("html", "head", "body", "main", "section", "div", "footer", "header"):
        assert counts.get(tag, 0) == 0, f"несбалансирован тег <{tag}>: {counts.get(tag)}"
