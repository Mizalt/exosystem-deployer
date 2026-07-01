"""«Домен из готового» — core-часть (ADR-057).

ЛК пушит список зон (`POST /api/integrations/dns`), UI публикации создаёт заявки на
A-записи (`POST /api/dns/requests`), исполнитель (ЛК) отмечает их complete. Деплоер
сам записи НЕ создаёт (API Рег.ру доступен только с egress ЛК).
"""


def _push_zones(client, zones):
    return client.post("/api/integrations/dns", json={"zones": zones})


def test_dns_integration_empty_by_default(auth_client):
    client, _ = auth_client
    r = client.get("/api/integrations/dns")
    assert r.status_code == 200
    assert r.json() == {"connected": False, "zones": []}


def test_push_zones_replaces_list(auth_client):
    client, _ = auth_client
    assert _push_zones(client, ["example.com", "foo.ru"]).status_code == 200
    r = client.get("/api/integrations/dns")
    assert r.json() == {"connected": True, "zones": ["example.com", "foo.ru"]}
    # Полная замена: старые зоны уходят, дубли схлопываются.
    assert _push_zones(client, ["bar.dev", "bar.dev"]).status_code == 200
    assert client.get("/api/integrations/dns").json()["zones"] == ["bar.dev"]
    # Пустой список = ЛК отключил Рег.ру → пикер гаснет.
    assert _push_zones(client, []).json() == {"connected": False, "zones": []}


def test_push_zones_rejects_bad_domain(auth_client):
    client, _ = auth_client
    r = _push_zones(client, ["bad domain; {}"])
    assert r.status_code == 422


def test_create_request_requires_known_zone(auth_client):
    client, _ = auth_client
    r = client.post("/api/dns/requests", json={"zone": "example.com", "subdomain": "app"})
    assert r.status_code == 400
    _push_zones(client, ["example.com"])
    r = client.post("/api/dns/requests", json={"zone": "example.com", "subdomain": "app"})
    assert r.status_code == 201
    body = r.json()
    assert body["fqdn"] == "app.example.com"
    assert body["status"] == "pending"


def test_create_request_idempotent_and_retries_after_error(auth_client):
    client, _ = auth_client
    _push_zones(client, ["example.com"])
    first = client.post("/api/dns/requests",
                        json={"zone": "example.com", "subdomain": "app"}).json()
    # Повторная заявка на тот же fqdn — та же строка (не дубль).
    second = client.post("/api/dns/requests",
                         json={"zone": "example.com", "subdomain": "app"}).json()
    assert second["id"] == first["id"]
    # Исполнитель провалил → новая заявка на тот же fqdn перезапускает (pending).
    client.post(f"/api/dns/requests/{first['id']}/complete",
                json={"status": "error", "note": "whitelist"})
    retried = client.post("/api/dns/requests",
                          json={"zone": "example.com", "subdomain": "app"}).json()
    assert retried["id"] == first["id"]
    assert retried["status"] == "pending"


def test_list_and_complete_flow(auth_client):
    client, _ = auth_client
    _push_zones(client, ["example.com"])
    req = client.post("/api/dns/requests",
                      json={"zone": "example.com", "subdomain": "app"}).json()
    pending = client.get("/api/dns/requests", params={"status": "pending"}).json()
    assert [p["id"] for p in pending] == [req["id"]]

    done = client.post(f"/api/dns/requests/{req['id']}/complete",
                       json={"status": "created", "note": "A app.example.com → 1.2.3.4"})
    assert done.status_code == 200
    assert done.json()["status"] == "created"
    assert client.get("/api/dns/requests", params={"status": "pending"}).json() == []
    assert client.get("/api/dns/requests").json()[0]["note"].startswith("A app")


def test_complete_unknown_request_404(auth_client):
    client, _ = auth_client
    r = client.post("/api/dns/requests/999/complete", json={"status": "created"})
    assert r.status_code == 404


def test_request_validates_subdomain(auth_client):
    client, _ = auth_client
    _push_zones(client, ["example.com"])
    r = client.post("/api/dns/requests",
                    json={"zone": "example.com", "subdomain": "bad sub;"})
    assert r.status_code == 422
