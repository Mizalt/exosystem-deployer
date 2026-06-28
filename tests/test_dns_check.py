"""DNS-чек панели: детерминированный матчинг по ВСЕМ A-записям (анти-мигание) + предупреждение
о лишних записях, которые ломают выпуск SSL (LE может проверить не тот сервер).

Регрессия из живого теста: чек брал addr_info[0] (одну случайную запись) → при нескольких
A-записях скакал ✅/❌. Теперь сервер «указан», если его IP ЕСТЬ среди всех A-записей.
"""
from app.routers.ssl import evaluate_dns_match


def test_single_matching_record():
    matches, warning = evaluate_dns_match("203.0.113.86", ["203.0.113.86"])
    assert matches is True
    assert warning is None


def test_server_ip_among_several_records_is_match_with_warning():
    # IP сервера ЕСТЬ среди записей → match детерминированно True (порядок не важен),
    # но лишние записи → предупреждение. IP — документационные (RFC 5737 TEST-NET-3).
    matches, warning = evaluate_dns_match(
        "203.0.113.86", ["203.0.113.86", "203.0.113.245", "203.0.113.21"])
    assert matches is True
    assert warning is not None
    assert "203.0.113.245" in warning and "203.0.113.21" in warning
    assert "203.0.113.86" not in warning.split("оставь только")[0]  # сервер не в списке «лишних»


def test_order_independence_no_flapping():
    # Та же зона в любом порядке резолвера даёт тот же результат (нет мигания).
    a = evaluate_dns_match("1.2.3.4", ["1.2.3.4", "9.9.9.9"])
    b = evaluate_dns_match("1.2.3.4", ["9.9.9.9", "1.2.3.4"])
    assert a == b
    assert a[0] is True


def test_no_record_points_here():
    matches, warning = evaluate_dns_match("1.2.3.4", ["9.9.9.9"])
    assert matches is False
    assert warning is None


def test_empty_records_not_match():
    matches, warning = evaluate_dns_match("1.2.3.4", [])
    assert matches is False
    assert warning is None


def test_no_server_ip_not_match():
    matches, warning = evaluate_dns_match(None, ["1.2.3.4"])
    assert matches is False
    assert warning is None
