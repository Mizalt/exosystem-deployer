"""Тесты авто-уборки неиспользуемых образов deployer-cache (ADR-025).

Проверяют: (1) тег образа детерминирован и совпадает у build/prune; (2) prune
удаляет только лишние теги, сохраняя «нужные» и занятые контейнером.
"""
import docker
import pytest

from app.services import docker_manager, orchestrator
from app import models


# --- compute_image_tag: единый источник формулы тега ------------------------ #

def test_compute_image_tag_deterministic_and_prefixed():
    cfg = {"base_image": None, "run_command": None, "internal_port": 80}
    t1 = docker_manager.compute_image_tag("hashA", cfg)
    t2 = docker_manager.compute_image_tag("hashA", cfg)
    assert t1 == t2
    assert t1.startswith("deployer-cache:")


def test_compute_image_tag_differs_on_config():
    a = docker_manager.compute_image_tag("h", {"internal_port": 80})
    b = docker_manager.compute_image_tag("h", {"internal_port": 9000})
    assert a != b


# --- prune_deployer_images -------------------------------------------------- #

class _FakeImage:
    def __init__(self, tags):
        self.tags = tags


class _FakeImages:
    def __init__(self, images):
        self._images = images
        self.removed = []
        self.in_use = set()

    def list(self, name=None):
        return self._images

    def remove(self, tag):
        if tag in self.in_use:
            raise docker.errors.APIError("conflict: image is being used")
        self.removed.append(tag)


class _FakeClient:
    def __init__(self, images):
        self.images = _FakeImages(images)


def test_prune_removes_only_unwanted(monkeypatch):
    keep = "deployer-cache:" + "a" * 32
    drop = "deployer-cache:" + "b" * 32
    fake = _FakeClient([_FakeImage([keep]), _FakeImage([drop])])
    monkeypatch.setattr(docker_manager, "client", fake)

    removed = docker_manager.prune_deployer_images({keep})

    assert removed == 1
    assert fake.images.removed == [drop]


def test_prune_skips_images_in_use(monkeypatch):
    drop = "deployer-cache:" + "c" * 32
    fake = _FakeClient([_FakeImage([drop])])
    fake.images.in_use.add(drop)  # занят контейнером → Docker не даст удалить
    monkeypatch.setattr(docker_manager, "client", fake)

    removed = docker_manager.prune_deployer_images(set())

    assert removed == 0  # конфликт проглочен, образ не «удалён»


def test_prune_ignores_non_deployer_images(monkeypatch):
    other = "nginx:1.25-alpine"
    fake = _FakeClient([_FakeImage([other])])
    monkeypatch.setattr(docker_manager, "client", fake)

    removed = docker_manager.prune_deployer_images(set())

    assert removed == 0
    assert fake.images.removed == []


def test_collect_wanted_tags_matches_build_formula(db, deployment):
    """Набор «нужных» тегов из БД совпадает с тегом, который соберёт build."""
    wanted = orchestrator.collect_wanted_image_tags(db)

    art = deployment.artifact
    expected = docker_manager.compute_image_tag(
        art.zip_hash,
        {"base_image": None, "run_command": None, "internal_port": 80},
    )
    assert wanted == {expected}
