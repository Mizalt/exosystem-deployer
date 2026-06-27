"""Тесты CRUD-слоя напрямую (app/crud.py), без API и Docker.

Используют in-memory сессию из фикстуры `db` (tests/conftest.py).
"""
from app import crud, schemas, models


# --------------------------------------------------------------------------- #
#  Группы портов
# --------------------------------------------------------------------------- #
def test_group_create_get_delete(db):
    g = crud.create_group(db, schemas.AppGroupCreate(name="g1", start_port=9001, end_port=9010))
    assert g.id is not None

    assert crud.get_group_by_name(db, "g1").id == g.id
    assert crud.get_group(db, g.id).name == "g1"
    assert [x.name for x in crud.get_groups(db)] == ["g1"]

    crud.delete_group(db, g.id)
    assert crud.get_group(db, g.id) is None


# --------------------------------------------------------------------------- #
#  Blueprint + Artifact
# --------------------------------------------------------------------------- #
def test_blueprint_and_artifact(db):
    bp = crud.create_blueprint(db, schemas.AppBlueprintCreate(name="api", description="demo"))
    assert crud.get_blueprint_by_name(db, "api").id == bp.id

    art = crud.create_artifact(db, schemas.ArtifactCreate(
        version_tag="v1", zip_hash="h1", stored_zip_path="uploads/h1.zip", blueprint_id=bp.id
    ))
    assert art.id is not None

    # joinedload подтягивает артефакты к blueprint
    loaded = crud.get_blueprint(db, bp.id)
    assert [a.version_tag for a in loaded.artifacts] == ["v1"]
    assert crud.get_artifact(db, art.id).zip_hash == "h1"


def test_blueprint_update_and_delete(db):
    bp = crud.create_blueprint(db, schemas.AppBlueprintCreate(name="old-name", description="old"))

    updated = crud.update_blueprint(db, bp.id, schemas.AppBlueprintUpdate(description="new desc"))
    assert updated.description == "new desc"
    assert updated.name == "old-name"  # exclude_unset: имя не затёрто

    renamed = crud.update_blueprint(db, bp.id, schemas.AppBlueprintUpdate(name="new-name"))
    assert renamed.name == "new-name"

    crud.delete_blueprint(db, bp.id)
    assert crud.get_blueprint(db, bp.id) is None


def test_artifact_delete_and_hash_count(db):
    bp = crud.create_blueprint(db, schemas.AppBlueprintCreate(name="art"))
    a1 = crud.create_artifact(db, schemas.ArtifactCreate(
        version_tag="v1", zip_hash="dup", stored_zip_path="uploads/dup.zip", blueprint_id=bp.id
    ))
    a2 = crud.create_artifact(db, schemas.ArtifactCreate(
        version_tag="v2", zip_hash="dup", stored_zip_path="uploads/dup.zip", blueprint_id=bp.id
    ))
    assert crud.count_artifacts_by_hash(db, "dup") == 2

    crud.delete_artifact(db, a1.id)
    assert crud.get_artifact(db, a1.id) is None
    assert crud.count_artifacts_by_hash(db, "dup") == 1  # a2 ещё держит файл

    crud.delete_artifact(db, a2.id)
    assert crud.count_artifacts_by_hash(db, "dup") == 0


def test_application_update_domain_and_ssl(db):
    bp = crud.create_blueprint(db, schemas.AppBlueprintCreate(name="upd"))
    art = crud.create_artifact(db, schemas.ArtifactCreate(
        version_tag="v1", zip_hash="h", stored_zip_path="uploads/h.zip", blueprint_id=bp.id
    ))
    crud.create_group(db, schemas.AppGroupCreate(name="ug", start_port=6001, end_port=6003))
    dep = crud.create_deployment(
        db,
        schemas.DeploymentCreate(target_replicas=1, group_name="ug", artifact_id=art.id),
        blueprint_id=bp.id,
    )
    app_obj = crud.create_application(db, schemas.ApplicationCreate(
        name="edit", domain="old.example.com", deployment_id=dep.id
    ))

    updated = crud.update_application(db, app_obj.id, schemas.ApplicationUpdate(
        domain="new.example.com", ssl_cert_name="new.example.com"
    ))
    assert updated.domain == "new.example.com"
    assert updated.ssl_cert_name == "new.example.com"


# --------------------------------------------------------------------------- #
#  Deployment
# --------------------------------------------------------------------------- #
def test_deployment_create_and_delete(db):
    bp = crud.create_blueprint(db, schemas.AppBlueprintCreate(name="svc"))
    art = crud.create_artifact(db, schemas.ArtifactCreate(
        version_tag="v1", zip_hash="h", stored_zip_path="uploads/h.zip", blueprint_id=bp.id
    ))
    crud.create_group(db, schemas.AppGroupCreate(name="backend", start_port=9001, end_port=9003))

    dep = crud.create_deployment(
        db,
        schemas.DeploymentCreate(target_replicas=2, group_name="backend", artifact_id=art.id),
        blueprint_id=bp.id,
    )
    assert dep.target_replicas == 2
    assert crud.get_deployment(db, dep.id).group_name == "backend"
    assert [d.id for d in crud.get_deployments(db)] == [dep.id]

    crud.delete_deployment(db, dep.id)
    assert crud.get_deployment(db, dep.id) is None


# --------------------------------------------------------------------------- #
#  Application + публичные пользователи
# --------------------------------------------------------------------------- #
def test_application_service_id_fallback_and_lookup(db):
    bp = crud.create_blueprint(db, schemas.AppBlueprintCreate(name="web"))
    art = crud.create_artifact(db, schemas.ArtifactCreate(
        version_tag="v1", zip_hash="h", stored_zip_path="uploads/h.zip", blueprint_id=bp.id
    ))
    crud.create_group(db, schemas.AppGroupCreate(name="frontend", start_port=8001, end_port=8003))
    dep = crud.create_deployment(
        db,
        schemas.DeploymentCreate(target_replicas=1, group_name="frontend", artifact_id=art.id),
        blueprint_id=bp.id,
    )

    # deployment_id не задан — должен взяться из service_id (слой совместимости)
    app_obj = crud.create_application(db, schemas.ApplicationCreate(
        name="shop", domain="shop.example.com", service_id=dep.id
    ))
    assert app_obj.deployment_id == dep.id
    assert crud.get_application_by_name(db, "shop").id == app_obj.id
    assert crud.get_application_by_domain(db, "shop.example.com").id == app_obj.id


def test_app_user_create_and_password_verify(db):
    bp = crud.create_blueprint(db, schemas.AppBlueprintCreate(name="auth"))
    art = crud.create_artifact(db, schemas.ArtifactCreate(
        version_tag="v1", zip_hash="h", stored_zip_path="uploads/h.zip", blueprint_id=bp.id
    ))
    crud.create_group(db, schemas.AppGroupCreate(name="grp", start_port=7001, end_port=7003))
    dep = crud.create_deployment(
        db,
        schemas.DeploymentCreate(target_replicas=1, group_name="grp", artifact_id=art.id),
        blueprint_id=bp.id,
    )
    app_obj = crud.create_application(db, schemas.ApplicationCreate(
        name="locked", domain="locked.example.com", deployment_id=dep.id
    ))

    user = crud.create_app_user(db, schemas.AppUserCreate(username="bob", password="secret"), app_obj.id)
    assert user.hashed_password != "secret"  # хранится хеш, не plaintext
    assert crud.get_app_user_by_username(db, app_obj.id, "bob").id == user.id
    assert crud.verify_password("secret", user.hashed_password) is True
    assert crud.verify_password("wrong", user.hashed_password) is False


# --------------------------------------------------------------------------- #
#  Пул занятых портов
# --------------------------------------------------------------------------- #
def test_get_all_used_ports(db):
    bp = crud.create_blueprint(db, schemas.AppBlueprintCreate(name="ports"))
    art = crud.create_artifact(db, schemas.ArtifactCreate(
        version_tag="v1", zip_hash="h", stored_zip_path="uploads/h.zip", blueprint_id=bp.id
    ))
    crud.create_group(db, schemas.AppGroupCreate(name="pg", start_port=9001, end_port=9003))
    dep = crud.create_deployment(
        db,
        schemas.DeploymentCreate(target_replicas=1, group_name="pg", artifact_id=art.id),
        blueprint_id=bp.id,
    )
    db.add_all([
        models.Instance(deployment_id=dep.id, assigned_port=9001, status="online"),
        models.Instance(deployment_id=dep.id, assigned_port=9002, status="online"),
    ])
    db.commit()

    assert crud.get_all_used_ports(db) == {9001, 9002}
