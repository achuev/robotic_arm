"""Точка входа ``gateway_node``.

Запускает HTTP/WS-сервер. Работает в двух режимах:

* ``ROBOT_BACKEND=sim`` (умолчание) — чистый Python, ROS не нужен вовсе;
* ``ROBOT_BACKEND=ros2`` — бэкенд поверх rclpy (появится на следующем этапе).

``rclpy`` здесь не импортируется ни на уровне модуля, ни при sim-режиме:
импорт живёт внутри ``backends/ros2.py``, который подключается лениво в
``create_app``.
"""

from __future__ import annotations

import logging
import sys

from .config import ConfigError, load_config

__all__ = ["main"]


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"конфигурация: {exc}", file=sys.stderr)
        return 2

    import uvicorn

    from .app import create_app

    uvicorn.run(
        create_app(config=config),
        host=config.host,
        port=config.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
