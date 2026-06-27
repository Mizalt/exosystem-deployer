# --- app/artifact_utils.py ---
"""Утилиты для загрузки версий (артефактов): авто-генерация тега версии и
извлечение метаданных из ZIP (VERSION / CHANGELOG). Без побочных эффектов и
зависимостей от Docker — легко тестируется.
"""
import io
import re
import tarfile
import zipfile
from datetime import datetime, timezone
from typing import Optional

# Имена файлов в архиве, из которых пытаемся вытащить версию/описание.
_VERSION_FILES = ("version", "version.txt")
_CHANGELOG_FILES = ("changelog.md", "changelog", "changelog.txt", "release_notes.md", "release_notes.txt")

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_INT_RE = re.compile(r"^v?(\d+)$")


def suggest_next_version(existing_tags) -> str:
    """Предлагает следующий тег версии по уже существующим.

    - semver (vX.Y.Z) → бамп patch (vX.Y.(Z+1)) у максимальной версии;
    - целочисленные (vN) → v(N+1);
    - иначе (или пусто) → дата-основанный fallback v{YYYY.MM.DD-HHMM}.
    """
    tags = [t.strip() for t in (existing_tags or []) if t and t.strip()]

    semvers = []
    for t in tags:
        m = _SEMVER_RE.match(t)
        if m:
            semvers.append(tuple(int(g) for g in m.groups()))
    if semvers:
        major, minor, patch = max(semvers)
        return f"v{major}.{minor}.{patch + 1}"

    ints = []
    for t in tags:
        m = _INT_RE.match(t)
        if m:
            ints.append(int(m.group(1)))
    if ints:
        return f"v{max(ints) + 1}"

    if not tags:
        return "v1.0.0"

    # Непонятный формат существующих тегов — отдаём дата-версию, чтобы не угадывать.
    return "v" + datetime.now(timezone.utc).strftime("%Y.%m.%d-%H%M")


# Разбор GitHub-URL: https/ssh, с .git и без, опц. /tree/<ref>.
_GITHUB_RE = re.compile(
    r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/tree/([\w./-]+))?/?$",
    re.IGNORECASE,
)


def parse_github_repo(url: str):
    """Извлекает (owner, repo, ref|None) из ссылки на GitHub-репозиторий.

    Поддерживает формы:
      https://github.com/owner/repo
      https://github.com/owner/repo.git
      git@github.com:owner/repo.git
      https://github.com/owner/repo/tree/<branch>
    Возвращает кортеж или None, если ссылка не похожа на GitHub-репозиторий.
    """
    m = _GITHUB_RE.search((url or "").strip())
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def tarball_to_zip(tar_bytes: bytes) -> bytes:
    """Конвертирует .tar.gz (GitHub codeload) в наш ZIP-формат.

    GitHub оборачивает содержимое в верхний каталог `{repo}-{ref}/` — срезаем его,
    чтобы структура совпала с обычной загрузкой ZIP (код приложения в корне).
    Symlink'и и спецфайлы пропускаем (берём только обычные файлы).
    """
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            # Срезаем первый компонент пути (верхний каталог GitHub-архива).
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            zf.writestr(parts[1], extracted.read())
    return out.getvalue()


def _read_member(zf: zipfile.ZipFile, names: tuple) -> Optional[str]:
    """Ищет в архиве файл с одним из имён (без учёта пути/регистра) и возвращает его текст."""
    by_basename = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        base = info.filename.rsplit("/", 1)[-1].lower()
        # Первый встретившийся выигрывает (обычно более «верхний»).
        by_basename.setdefault(base, info.filename)
    for wanted in names:
        if wanted in by_basename:
            try:
                raw = zf.read(by_basename[wanted])
                return raw.decode("utf-8", errors="replace")
            except Exception:
                return None
    return None


def inspect_zip(zip_bytes: bytes) -> dict:
    """Достаёт из ZIP подсказки для формы загрузки версии.

    Возвращает {"version": Optional[str], "description": Optional[str]}.
    version — первая строка файла VERSION; description — содержимое CHANGELOG/
    RELEASE_NOTES (усечённое). Любые ошибки чтения архива молча игнорируются.
    """
    result = {"version": None, "description": None}
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            version_text = _read_member(zf, _VERSION_FILES)
            if version_text:
                first_line = next((ln.strip() for ln in version_text.splitlines() if ln.strip()), None)
                if first_line:
                    result["version"] = first_line[:64]

            changelog_text = _read_member(zf, _CHANGELOG_FILES)
            if changelog_text:
                cleaned = changelog_text.strip()
                if cleaned:
                    result["description"] = cleaned[:1000]
    except (zipfile.BadZipFile, OSError):
        pass
    return result
