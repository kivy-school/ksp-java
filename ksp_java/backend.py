from __future__ import annotations

from io import BytesIO
from json import JSONDecodeError
from pathlib import Path
import json
import tarfile
import tempfile
import zipfile
import base64
import csv
import hashlib
from io import StringIO

from hatchling.build import (
    build_editable as _hatchling_build_editable,
    build_sdist as _hatchling_build_sdist,
    build_wheel as _hatchling_build_wheel,
    get_requires_for_build_editable as _hatchling_get_requires_for_build_editable,
    get_requires_for_build_sdist as _hatchling_get_requires_for_build_sdist,
    get_requires_for_build_wheel as _hatchling_get_requires_for_build_wheel,
    prepare_metadata_for_build_editable as _hatchling_prepare_metadata_for_build_editable,
    prepare_metadata_for_build_wheel as _hatchling_prepare_metadata_for_build_wheel,
)

import tomllib

JAVA_ARCHIVE_PREFIX = ".java"


def _parse_path_config_value(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, (list, tuple)):
        raw_values = [str(item) for item in value]
    else:
        raw_values = [str(value)]

    paths: list[str] = []
    for raw in raw_values:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                paths.extend(str(item).strip() for item in parsed if str(item).strip())
                continue
        except (JSONDecodeError, TypeError, ValueError):
            pass

        paths.extend(part.strip() for part in raw.split(",") if part.strip())
    return paths


def _read_pyproject_java_paths(project_root: Path) -> list[str]:
    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        return []

    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    tool_data = data.get("tool", {}).get("ksp-java", {})
    return _parse_path_config_value(tool_data.get("java-paths"))


def get_java_source_dirs(config_settings=None, project_root: Path | None = None) -> list[Path]:
    root = Path.cwd() if project_root is None else Path(project_root)
    config_settings = config_settings or {}

    configured_paths = _read_pyproject_java_paths(root)
    configured_paths.extend(
        _parse_path_config_value(config_settings.get("java_paths"))
        + _parse_path_config_value(config_settings.get("java-paths"))
        + _parse_path_config_value(config_settings.get("java-path"))
        + _parse_path_config_value(config_settings.get("ksp-java.java_paths"))
    )

    unique_paths: list[Path] = []
    seen: set[Path] = set()
    for configured_path in configured_paths:
        path = (root / configured_path).resolve()
        if path in seen:
            continue
        if not path.exists():
            raise FileNotFoundError(
                f"Configured java path does not exist: {configured_path} (resolved to {path})"
            )
        if not path.is_dir():
            raise NotADirectoryError(
                f"Configured java path is not a directory: {configured_path} (resolved to {path})"
            )
        seen.add(path)
        unique_paths.append(path)
    return unique_paths


def _collect_java_files(java_dirs: list[Path]) -> list[tuple[Path, str]]:
    java_files: list[tuple[Path, str]] = []
    for source_dir in java_dirs:
        for source_file in source_dir.rglob("*"):
            if not source_file.is_file():
                continue
            rel = source_file.relative_to(source_dir).as_posix()
            java_files.append((source_file, f"{JAVA_ARCHIVE_PREFIX}/{rel}"))
    return java_files


def _get_pypi_hash(data: bytes) -> str:
    """Calculates the urlsafe base64 sha256 hash required by PEP 376 / PEP 427."""
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def add_java_sources_to_wheel(wheel_path: Path, java_dirs: list[Path]) -> None:
    if not java_dirs:
        return
    java_files = _collect_java_files(java_dirs)
    if not java_files:
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_wheel_path = Path(tmpdir) / "rewritten.whl"

        with zipfile.ZipFile(wheel_path, "r") as old_wheel:
            record_path = next(
                (
                    name
                    for name in old_wheel.namelist()
                    if name.endswith(".dist-info/RECORD")
                ),
                None,
            )

            if not record_path:
                raise FileNotFoundError(
                    "Could not find RECORD file in the generated wheel."
                )

            record_data = old_wheel.read(record_path).decode("utf-8")

            with zipfile.ZipFile(
                temp_wheel_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as new_wheel:
                for item in old_wheel.infolist():
                    if item.filename != record_path:
                        new_wheel.writestr(item, old_wheel.read(item.filename))

                record_io = StringIO()
                csv_writer = csv.writer(record_io, lineterminator="\n")

                csv_reader = csv.reader(StringIO(record_data))
                for row in csv_reader:
                    csv_writer.writerow(row)

                for source_file, archive_name in java_files:
                    file_bytes = source_file.read_bytes()
                    new_wheel.writestr(archive_name, file_bytes)

                    file_hash = _get_pypi_hash(file_bytes)
                    file_size = len(file_bytes)

                    csv_writer.writerow([archive_name, file_hash, file_size])

                new_wheel.writestr(record_path, record_io.getvalue().encode("utf-8"))

        temp_wheel_path.replace(wheel_path)


def add_java_sources_to_sdist(sdist_path: Path, java_dirs: list[Path]) -> None:
    if not java_dirs:
        return
    java_files = _collect_java_files(java_dirs)
    if not java_files:
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_sdist_path = Path(tmpdir) / "rewritten.tar.gz"
        with tarfile.open(sdist_path, "r:gz") as source_tar:
            members = source_tar.getmembers()
            root_prefix = members[0].name.split("/", 1)[0] if members else ""

            with tarfile.open(temp_sdist_path, "w:gz") as new_tar:
                for member in members:
                    if member.isfile():
                        extracted = source_tar.extractfile(member)
                        if extracted is None:
                            continue
                        with extracted:
                            new_tar.addfile(member, extracted)
                    else:
                        new_tar.addfile(member)
                for source_file, archive_name in java_files:
                    tar_info = tarfile.TarInfo(
                        name=f"{root_prefix}/{archive_name}" if root_prefix else archive_name
                    )
                    file_bytes = source_file.read_bytes()
                    tar_info.size = len(file_bytes)
                    new_tar.addfile(tar_info, BytesIO(file_bytes))

        temp_sdist_path.replace(sdist_path)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    java_dirs = get_java_source_dirs(config_settings=config_settings)
    wheel_name = _hatchling_build_wheel(
        wheel_directory=wheel_directory,
        config_settings=config_settings,
        metadata_directory=metadata_directory,
    )
    add_java_sources_to_wheel(Path(wheel_directory) / wheel_name, java_dirs)
    return wheel_name


def build_sdist(sdist_directory, config_settings=None):
    java_dirs = get_java_source_dirs(config_settings=config_settings)
    sdist_name = _hatchling_build_sdist(
        sdist_directory=sdist_directory,
        config_settings=config_settings,
    )
    add_java_sources_to_sdist(Path(sdist_directory) / sdist_name, java_dirs)
    return sdist_name


def get_requires_for_build_wheel(config_settings=None):
    return _hatchling_get_requires_for_build_wheel(config_settings=config_settings)


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return _hatchling_prepare_metadata_for_build_wheel(
        metadata_directory=metadata_directory,
        config_settings=config_settings,
    )


def get_requires_for_build_sdist(config_settings=None):
    return _hatchling_get_requires_for_build_sdist(config_settings=config_settings)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    java_dirs = get_java_source_dirs(config_settings=config_settings)
    wheel_name = _hatchling_build_editable(
        wheel_directory=wheel_directory,
        config_settings=config_settings,
        metadata_directory=metadata_directory,
    )
    add_java_sources_to_wheel(Path(wheel_directory) / wheel_name, java_dirs)
    return wheel_name


def get_requires_for_build_editable(config_settings=None):
    return _hatchling_get_requires_for_build_editable(config_settings=config_settings)


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
    return _hatchling_prepare_metadata_for_build_editable(
        metadata_directory=metadata_directory,
        config_settings=config_settings,
    )
