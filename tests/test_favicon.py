"""Favicon с зелёной точкой в ЛК и панели (задание R, фидбек владельца).

На лендинге у вкладки браузера иконка с зелёной точкой (marketing/landing/.../favicon.svg),
а в ЛК и панели favicon не было вовсе. Единый бренд-favicon (тёмный ромб + зелёная
точка) кладём в два места статики и подключаем во всех HTML-каркасах:
  • панель (ЯДРО): static/favicon.svg → /static/favicon.svg (существующий mount);
  • ЛК (cloud):    app/cloud/static/assets/favicon.svg → /assets/favicon.svg.
"""
import pathlib

from fastapi.testclient import TestClient

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Зелёная точка — сигнатура иконки лендинга (circle fill #6dd58c). Её наличие
# гарантирует, что скопировали именно брендовый favicon с зелёной точкой.
_GREEN_DOT = 'fill="#6dd58c"'


def test_favicon_files_exist_with_green_dot():
    """Favicon-файл лежит в ОБОИХ местах статики и несёт зелёную точку (тот же бренд)."""
    for rel in ("static/favicon.svg", "app/cloud/static/assets/favicon.svg"):
        svg = (_ROOT / rel).read_text(encoding="utf-8")
        assert "<svg" in svg, f"{rel}: не SVG"
        assert _GREEN_DOT in svg, f"{rel}: нет зелёной точки {_GREEN_DOT}"
        assert "﻿" not in svg, f"{rel}: BOM в SVG"


def test_panel_index_head_links_favicon():
    """Панель (ЯДРО): корневой index.html подключает favicon через /static-mount."""
    html = (_ROOT / "index.html").read_text(encoding="utf-8")
    assert 'rel="icon"' in html
    assert 'type="image/svg+xml"' in html
    assert "/static/favicon.svg" in html


def test_cloud_html_shells_link_favicon():
    """ЛК (cloud): все три каркаса (ЛК/студия/админка) подключают favicon через /assets."""
    for page in ("index.html", "studio.html", "admin.html"):
        html = (_ROOT / "app" / "cloud" / "static" / page).read_text(encoding="utf-8")
        assert 'rel="icon"' in html, f"{page}: нет link rel=icon"
        assert 'type="image/svg+xml"' in html, f"{page}: не SVG-иконка"
        assert "/assets/favicon.svg" in html, f"{page}: favicon не через /assets"


def test_panel_serves_favicon_200():
    """Панель реально отдаёт favicon через существующий /static-mount (200 + SVG)."""
    import main

    r = TestClient(main.app).get("/static/favicon.svg")
    assert r.status_code == 200, r.text
    assert "svg" in r.headers["content-type"]
    assert _GREEN_DOT in r.text


def test_cloud_serves_favicon_200(cloud_env):
    """ЛК реально отдаёт favicon через существующий /assets-mount (200 + SVG)."""
    _, _Session, client = cloud_env
    r = client.get("/assets/favicon.svg")
    assert r.status_code == 200, r.text
    assert "svg" in r.headers["content-type"]
    assert _GREEN_DOT in r.text
