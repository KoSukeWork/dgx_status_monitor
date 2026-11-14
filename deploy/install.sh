#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    printf '请使用 sudo bash deploy/install.sh 运行安装程序。\n' >&2
    exit 1
fi

SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR=/opt/dgx-status-monitor
install -d "$INSTALL_DIR/src/dgx_status_monitor" /etc/default /etc/systemd/system
install -m 644 "$SOURCE_ROOT/pyproject.toml" "$SOURCE_ROOT/uv.lock" "$INSTALL_DIR/"
install -m 644 "$SOURCE_ROOT/src/dgx_status_monitor/"*.py "$INSTALL_DIR/src/dgx_status_monitor/"

if command -v uv >/dev/null 2>&1; then
    (cd "$INSTALL_DIR" && uv sync --frozen --no-dev --python /usr/bin/python3)
else
    if ! python3 -m venv "$INSTALL_DIR/.venv" 2>/dev/null; then
        printf '无法创建 Python 虚拟环境。请检查 python3、目录权限和磁盘空间；在 Ubuntu 上可运行 sudo apt install python3-venv 后重试。\n' >&2
        exit 1
    fi
    "$INSTALL_DIR/.venv/bin/python" -m pip install "$INSTALL_DIR"
fi

install -m 644 "$SOURCE_ROOT/deploy/dgx-status-monitor.service" /etc/systemd/system/dgx-status-monitor.service
if [ ! -e /etc/default/dgx-status-monitor ]; then
    install -m 644 "$SOURCE_ROOT/deploy/dgx-status-monitor.env" /etc/default/dgx-status-monitor
fi
systemctl daemon-reload
systemctl enable dgx-status-monitor.service
systemctl restart dgx-status-monitor.service
printf '安装完成。使用 systemctl status dgx-status-monitor 查看状态。\n'
