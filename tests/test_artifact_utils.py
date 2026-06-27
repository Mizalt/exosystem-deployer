"""Тесты утилит загрузки версий (app/artifact_utils.py): авто-тег и инспекция ZIP."""
import io
import tarfile
import zipfile

from app import artifact_utils


def _make_tar_gz(files: dict, top: str = "repo-main") -> bytes:
    """Имитирует GitHub codeload-архив: файлы под верхним каталогом `{repo}-{ref}/`."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_suggest_next_version_semver():
    assert artifact_utils.suggest_next_version(["v1.0.0", "v1.2.3", "v1.1.0"]) == "v1.2.4"
    assert artifact_utils.suggest_next_version(["1.0.0"]) == "v1.0.1"


def test_suggest_next_version_int_and_empty():
    assert artifact_utils.suggest_next_version(["v3", "v1"]) == "v4"
    assert artifact_utils.suggest_next_version([]) == "v1.0.0"
    assert artifact_utils.suggest_next_version(None) == "v1.0.0"


def test_suggest_next_version_unknown_format_is_date():
    out = artifact_utils.suggest_next_version(["release-foo"])
    assert out.startswith("v") and "." in out  # дата-фолбэк vYYYY.MM.DD-HHMM


def test_inspect_zip_reads_version_and_changelog():
    data = _make_zip({
        "VERSION": "v2.5.0\n",
        "CHANGELOG.md": "# Changes\n- fixed bug\n",
        "app/main.py": "print('x')",
    })
    meta = artifact_utils.inspect_zip(data)
    assert meta["version"] == "v2.5.0"
    assert "fixed bug" in meta["description"]


def test_inspect_zip_handles_missing_and_bad():
    assert artifact_utils.inspect_zip(_make_zip({"a.py": "1"})) == {"version": None, "description": None}
    assert artifact_utils.inspect_zip(b"not a zip") == {"version": None, "description": None}


def test_parse_github_repo_forms():
    assert artifact_utils.parse_github_repo("https://github.com/owner/repo") == ("owner", "repo", None)
    assert artifact_utils.parse_github_repo("https://github.com/owner/repo.git") == ("owner", "repo", None)
    assert artifact_utils.parse_github_repo("git@github.com:owner/repo.git") == ("owner", "repo", None)
    assert artifact_utils.parse_github_repo("https://github.com/owner/repo/tree/dev") == ("owner", "repo", "dev")
    assert artifact_utils.parse_github_repo("https://gitlab.com/owner/repo") is None
    assert artifact_utils.parse_github_repo("") is None


def test_tarball_to_zip_strips_top_dir_and_preserves_content():
    tar = _make_tar_gz({"VERSION": "v9.9.9\n", "app/main.py": "print('hi')"}, top="repo-feature")
    zip_bytes = artifact_utils.tarball_to_zip(tar)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        # Верхний каталог GitHub-архива срезан — код в корне.
        assert names == {"VERSION", "app/main.py"}
        assert zf.read("app/main.py").decode() == "print('hi')"
    # inspect_zip работает на полученном из tarball ZIP (сквозная проверка импорта).
    assert artifact_utils.inspect_zip(zip_bytes)["version"] == "v9.9.9"
