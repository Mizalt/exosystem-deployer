"""Catchall nginx должен отдавать ACME HTTP-01 challenge (анти-403 при выпуске SSL).

Регрессия из живого теста: панельный/приложенческий SSL ловил 403, т.к. challenge
для домена без своего server-блока попадал в catchall с `return 403`. Catchall теперь
обслуживает `/.well-known/acme-challenge/` из webroot ДО возврата 403.
"""
from app.services.nginx_manager import CATCHALL_CONFIG_TEMPLATE


def test_catchall_serves_acme_challenge():
    t = CATCHALL_CONFIG_TEMPLATE
    assert "location /.well-known/acme-challenge/" in t
    assert "root /var/www/acme_challenge;" in t


def test_catchall_acme_before_403():
    t = CATCHALL_CONFIG_TEMPLATE
    # ACME-локация должна идти раньше `return 403`, иначе challenge не отдаётся.
    assert t.index("acme-challenge") < t.index("return 403")
