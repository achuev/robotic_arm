"""Сборка пакета.

Пакет двухголовый:

* обычный pip-пакет — метаданные, зависимости и entry_point живут в
  ``pyproject.toml`` (``pip install -e .`` работает на macOS без ROS);
* ROS 2 ``ament_python`` — colcon требует ``setup.py`` и регистрации пакета в
  ament-индексе, поэтому здесь остаются только ``data_files``. Всё остальное
  setuptools берёт из ``pyproject.toml``, дублировать нельзя — иначе поля
  конфликтуют.
"""

from setuptools import setup

PACKAGE_NAME = "so101_gateway"

setup(
    data_files=[
        # регистрация в ament index — без неё `ros2 run` пакет не найдёт
        ("share/ament_index/resource_index/packages", ["resource/" + PACKAGE_NAME]),
        ("share/" + PACKAGE_NAME, ["package.xml"]),
    ],
)
