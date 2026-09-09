"""Гарантии упаковки.

Главная из них: ``rclpy`` не должен импортироваться на уровне модуля нигде,
кроме ``backends/ros2.py`` — иначе пакет перестанет запускаться на macOS.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import so101_gateway

PACKAGE_DIR = Path(so101_gateway.__file__).resolve().parent
GATEWAY_DIR = PACKAGE_DIR.parent
ROS_ONLY_MODULE = "ros2.py"


def python_files():
    return sorted(PACKAGE_DIR.rglob("*.py"))


def module_level_imports(path: Path):
    """Имена модулей, импортируемых на верхнем уровне файла."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []
    for node in tree.body:                       # только верхний уровень
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.append(node.module)
    return names


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_no_module_level_rclpy_import(path: Path):
    if path.name == ROS_ONLY_MODULE:
        pytest.skip("backends/ros2.py — единственное место, где rclpy разрешён")
    offenders = [n for n in module_level_imports(path) if n.split(".")[0] == "rclpy"]
    assert not offenders, f"{path} импортирует rclpy на уровне модуля: {offenders}"


def test_importing_the_whole_package_does_not_pull_in_ros():
    """Импорт всех модулей пакета не должен требовать ROS."""
    code = (
        "import importlib, pkgutil, sys\n"
        "import so101_gateway\n"
        "for m in pkgutil.walk_packages(so101_gateway.__path__, 'so101_gateway.'):\n"
        "    if m.name.endswith('.ros2'):\n"
        "        continue\n"
        "    importlib.import_module(m.name)\n"
        "assert 'rclpy' not in sys.modules, 'rclpy утёк в импорт'\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=str(GATEWAY_DIR)
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_creating_the_app_does_not_import_rclpy(config):
    from so101_gateway.app import create_app

    create_app(config=config.replace(admin_token="t"))
    assert "rclpy" not in sys.modules


# --------------------------------------------------------------------------- #
# файлы пакета
# --------------------------------------------------------------------------- #

def test_pyproject_declares_gateway_node_entry_point():
    text = (GATEWAY_DIR / "pyproject.toml").read_text(encoding="utf-8")
    assert "[project.scripts]" in text
    assert "gateway_node" in text
    assert 'so101_gateway.node:main' in text


def test_pyproject_does_not_depend_on_rclpy():
    text = (GATEWAY_DIR / "pyproject.toml").read_text(encoding="utf-8")
    dependencies = text.split("[project.optional-dependencies]")[0]
    assert '"rclpy' not in dependencies


def test_package_xml_is_a_valid_ament_python_package():
    root = ET.parse(GATEWAY_DIR / "package.xml").getroot()
    assert root.findtext("name") == "so101_gateway"
    build_type = root.find("export/build_type")
    assert build_type is not None and build_type.text == "ament_python"


def test_ament_resource_marker_exists():
    assert (GATEWAY_DIR / "resource" / "so101_gateway").exists()


def test_node_entry_point_is_importable_and_callable():
    node = importlib.import_module("so101_gateway.node")
    assert callable(node.main)


def test_node_main_reports_missing_admin_token(monkeypatch, capsys):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    from so101_gateway import node

    assert node.main() == 2
    assert "ADMIN_TOKEN" in capsys.readouterr().err
