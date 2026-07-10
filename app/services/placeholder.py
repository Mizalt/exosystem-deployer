# --- app/services/placeholder.py ---
"""Брендированная заглушка окна неготовности сервиса (задача #3, ADR-142).

Когда пользователь привязывает НОВЫЙ домен, DNS/vhost активны сразу, а контейнер
ещё поднимается (или SSL только что выпущен, но реплика ещё не online). В это
окно посетитель раньше видел голые 502/503 — теперь прокси-гейт
(app/routers/proxy.py) отдаёт браузерным навигациям эту страницу.

🔴 Контракт безопасности (нулевая инъекционная поверхность):
- Страница ГЕНЕРИЧЕСКАЯ: никакой подстановки данных запроса (Host, домен, путь,
  заголовки) и никаких внутренностей (имя приложения, статус сбоя, стектрейс,
  внутренние хосты). Reflected-контент запрещён — поэтому рендер БЕЗ параметров.
- White-label: текст не выдаёт платформу — домен принадлежит пользователю
  платформы, заглушка выглядит как «его» будущая страница.

Самодостаточность: HTML со встроенным CSS, без внешних ссылок/шрифтов/картинок —
страница отдаётся с домена, на котором ещё ничего нет. Авто-обновление через
<meta http-equiv="refresh"> ~10с: когда реплика станет online (reconcile
оркестратора каждые 5с), очередная перезагрузка попадёт на реальный сервис.
"""

# Маркер для тестов/отладки — невидимый атрибут на <html>. Не несёт данных запроса.
PLACEHOLDER_MARKER = "warmup-placeholder"

_PLACEHOLDER_HTML = """<!DOCTYPE html>
<html lang="ru" data-page="warmup-placeholder">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="10">
<title>Скоро открытие</title>
<style>
  :root { color-scheme: dark; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    display: flex; align-items: center; justify-content: center;
    background: radial-gradient(1200px 800px at 70% -10%, #1c2540 0%, #0d1117 55%) #0d1117;
    color: #e6edf3;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    text-align: center;
    padding: 24px;
  }
  .card { max-width: 560px; }
  .pulse {
    width: 14px; height: 14px; border-radius: 50%;
    background: #4c9aff; margin: 0 auto 28px;
    animation: pulse 1.6s ease-in-out infinite;
  }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(76, 154, 255, .55); }
    70%  { box-shadow: 0 0 0 22px rgba(76, 154, 255, 0); }
    100% { box-shadow: 0 0 0 0 rgba(76, 154, 255, 0); }
  }
  h1 {
    font-size: clamp(1.6rem, 5vw, 2.4rem);
    font-weight: 700; letter-spacing: -.02em; margin-bottom: 14px;
  }
  p { font-size: 1.05rem; line-height: 1.6; color: #9aa7b4; }
  .hint { margin-top: 26px; font-size: .85rem; color: #5c6773; }
  @media (prefers-reduced-motion: reduce) { .pulse { animation: none; } }
</style>
</head>
<body>
  <div class="card">
    <div class="pulse" aria-hidden="true"></div>
    <h1>Тут скоро будет что-то новенькое</h1>
    <p>Сервис почти готов и вот-вот откроется.</p>
    <p class="hint">Страница обновится автоматически — можно ничего не нажимать.</p>
  </div>
</body>
</html>
"""


def render_placeholder() -> str:
    """Возвращает HTML заглушки. НАМЕРЕННО без параметров: никакие данные запроса
    в страницу не попадают (см. контракт безопасности в докстринге модуля)."""
    return _PLACEHOLDER_HTML
