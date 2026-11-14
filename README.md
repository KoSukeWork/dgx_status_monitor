# DGX GB10 状态监控

轻量的只读服务，每 500 ms 采样一次整体 CPU、统一内存和 GPU 状态，在局域网通过 HTTP 提供最新快照。不保存历史，不提供逐核或进程信息。默认监听 `0.0.0.0:9500`；接口没有鉴权，仅适用于可信网络。

English summary: [see below](#english).

## 快速开始

在 GB10 上执行：

```bash
git clone https://github.com/KoSukeWork/dgx_status_monitor.git
cd dgx_status_monitor
sudo bash deploy/install.sh
curl http://127.0.0.1:9500/healthz
```

返回 `{"status":"ok",...}` 就说明服务正常。用 `hostname -I` 查到 GB10 的局域网 IP，然后在局域网的其他机器上调用：

```bash
curl http://<GB10的IP>:9500/api/v1/snapshot
```

连不上时，在 GB10 上运行 `ss -ltn | grep 9500`，确认监听的是 `0.0.0.0:9500`。安装完成后，建议按[部署后验证](#gb10-部署后验证与排查)核对一次 `gpu_used_mib`。

## 安装与运行

需要 Python 3.10 或以上，GB10 推荐使用 DGX OS / Ubuntu 24.04 自带的 Python 3.12 和已安装的 NVIDIA 驱动。运行时唯一的第三方依赖是 `nvidia-ml-py`。在项目目录下开发或手动运行：

```bash
uv sync
uv run dgx-status-monitor
# 或者
uv run python -m dgx_status_monitor
```

未安装 uv 时也可以使用标准虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/dgx-status-monitor
```

直接运行时按 Ctrl+C 结束。Windows 上可以启动和调用接口；没有 `/proc` 或无法加载 NVML 时，对应字段为 `null`。

### 安装 systemd 服务

在 GB10 上进入项目目录（`git clone` 下来的 `dgx_status_monitor`），执行：

```bash
sudo bash deploy/install.sh
sudo systemctl status dgx-status-monitor
sudo journalctl -u dgx-status-monitor -f
```

脚本将程序安装到 `/opt/dgx-status-monitor`；若 `sudo` 的 PATH 中有 uv，使用锁文件安装，否则使用 `python3 -m venv` 和 pip（Ubuntu 可能需要先安装 `python3-venv`）。脚本启用并启动服务；重新安装会重启服务。配置文件 `/etc/default/dgx-status-monitor` 已存在时不会被覆盖。

可编辑 `/etc/default/dgx-status-monitor`，修改后运行 `sudo systemctl restart dgx-status-monitor`：

| 命令行参数 | 环境变量 | 默认值 |
| --- | --- | --- |
| `--host` | `DGX_MONITOR_HOST` | `0.0.0.0` |
| `--port` | `DGX_MONITOR_PORT` | `9500` |
| `--interval-ms` | `DGX_MONITOR_INTERVAL_MS` | `500` |
| `--stale-ms` | `DGX_MONITOR_STALE_MS` | `2000` |
| `--log-level` | `DGX_MONITOR_LOG_LEVEL` | `INFO` |

命令行参数优先于环境变量，环境变量优先于默认值。日志输出到 stderr，systemd 会将其收集到 journal。

升级到新版本：

```bash
cd dgx_status_monitor
git pull
sudo bash deploy/install.sh
```

脚本会更新 `/opt/dgx-status-monitor` 中的程序并重启服务，`/etc/default/dgx-status-monitor` 里的配置会保留。

卸载服务和程序：

```bash
sudo systemctl disable --now dgx-status-monitor
sudo rm -f /etc/systemd/system/dgx-status-monitor.service
sudo systemctl daemon-reload
sudo rm -rf /opt/dgx-status-monitor
# 如不再需要配置，再运行：sudo rm -f /etc/default/dgx-status-monitor
```

## HTTP API

获取最新快照（GB10 的局域网 IP 可以在 GB10 上用 `hostname -I` 查看）：

```bash
curl http://<GB10的IP>:9500/api/v1/snapshot
curl http://<GB10的IP>:9500/healthz
```

Python 示例（标准库）：

```python
import json
from urllib.request import urlopen

with urlopen("http://<GB10的IP>:9500/api/v1/snapshot", timeout=5) as response:
    snapshot = json.load(response)
print(snapshot["cpu"]["percent"], snapshot["memory"]["gpu_used_mib"])
```

快照示例：

```json
{
  "ts": 1763039658.512,
  "cpu": {"percent": 37.5, "load_avg": [3.1, 2.8, 2.5]},
  "memory": {"total_mib": 121000, "used_mib": 65000, "available_mib": 56000,
             "percent": 53.7, "swap_total_mib": 0, "swap_used_mib": 0, "gpu_used_mib": 31336},
  "gpu": {"util_percent": 0, "sm_clock_mhz": 2398, "power_w": 10.4, "temp_c": 43}
}
```

| 字段 | 含义 |
| --- | --- |
| `ts` | 采样时的 Unix 时间，秒，保留三位小数 |
| `cpu.percent` | 两次采样间全部 CPU 的忙碌比例，含小数一位 |
| `cpu.load_avg` | 系统 1、5、15 分钟的负载均值 |
| `memory.total_mib`, `used_mib`, `available_mib` | 统一内存总量、已用量（总量减可用量）、可用量，整数 MiB |
| `memory.percent` | 已用统一内存的百分比，含小数一位 |
| `memory.swap_total_mib`, `swap_used_mib` | 交换空间总量和已用量，整数 MiB |
| `memory.gpu_used_mib` | NVML 记录的 GPU 进程内存总和，整数 MiB；计算及图形进程按 PID 去重并取较大值 |
| `gpu.util_percent` | GPU 利用率百分比 |
| `gpu.sm_clock_mhz` | SM 频率，MHz |
| `gpu.power_w` | 功耗，瓦，保留两位小数 |
| `gpu.temp_c` | GPU 温度，摄氏度 |

MiB 为 1024² 字节。无法取得的字段为 `null`，所有键仍然存在。启动后首次采样前，快照接口返回 503 和 `{"error":"warming_up"}`。`/healthz` 对新鲜快照返回 200 和 `{"status":"ok","age_ms":123,"sample_duration_ms":1.8}`，快照过期或尚未生成时返回 503，状态为 `stale`。支持 GET、HEAD 和 OPTIONS；其他方法返回 405。响应包含 CORS 和禁止缓存的头。

GPU 利用率是驱动在一个时间窗口内的平均值，不是瞬时值。

### 关于 `gpu_used_mib`

- GB10 使用统一内存，`gpu_used_mib` 是 `memory.used_mib` 的**一部分**，不是额外的内存。
- 它只统计驱动记在 GPU 进程名下的内存，主要是 CUDA 分配的内存；普通 `malloc` 申请后交给 GPU 使用的内存可能没有计入。
- 取值规则：
  - 没有 GPU 进程：`0`。
  - 有 GPU 进程，但所有进程的显存值都读不到：`null`。
  - 只有部分显存值读得到：返回读得到的部分之和，结果可能偏低；进入这种状态时日志记录一次 WARNING。
  - 计算进程和图形进程两个列表都读取失败：`null`；只有一个列表读取成功时，只统计这一个列表。
  - 同一个 PID 同时出现在两个列表里时只算一次，取较大的值。

## GB10 部署后验证与排查

1. 查看 `sudo systemctl status dgx-status-monitor` 和 `sudo journalctl -u dgx-status-monitor -n 100 --no-pager`，确认服务正常启动并识别 GPU。
2. 比较 `curl http://127.0.0.1:9500/api/v1/snapshot` 返回的 `memory.gpu_used_mib` 与 `sudo nvidia-smi` 的 Processes 中各进程显存之和。相同 PID 同时出现于计算和图形列表时，服务只取该 PID 的较大值。分别运行不同用户的 GPU 工作负载，确认默认的 `DynamicUser` 能看到所有用户的进程；MiB 取整和采样时刻不同可能产生少量差异。
3. 若 GPU 字段为 `null`，先检查驱动、`nvidia-smi` 和日志，再逐项注释 unit 中的 `NoNewPrivileges`、`ProtectSystem`、`ProtectHome`、`PrivateTmp`，每次执行 `sudo systemctl daemon-reload` 与 `sudo systemctl restart dgx-status-monitor` 以定位影响项。完成排查后恢复不影响运行的设置。
4. 如果 `DynamicUser` 看不到全部 GPU 进程，可使用 `sudo systemctl edit dgx-status-monitor` 添加以下覆盖项，以 root 运行，然后重启服务：

   ```ini
   [Service]
   DynamicUser=no
   User=root
   ```

   也可将 `User=root` 换成已有用户并增加 `Group=该用户的组`；确保该用户有访问 `/dev/nvidia*` 和所需进程信息的权限。指定用户是否能看到所有用户的 GPU 进程，仍需按第 2 步验证。

## English

A lightweight, read-only HTTP service for NVIDIA DGX GB10. It samples overall CPU, unified memory, and GPU usage every 500 ms and serves the latest snapshot as JSON.

- **Why:** GB10 uses unified memory, so `nvidia-smi` and NVML report framebuffer memory as N/A. This service reports GPU memory (`memory.gpu_used_mib`) as the sum of per-process GPU memory from NVML (compute and graphics processes, deduplicated by PID). It is part of `memory.used_mib`, not additional memory.
- **Endpoints:** `GET /api/v1/snapshot` returns the latest values; `GET /healthz` returns 200 when the last sample is at most 2 s old, otherwise 503. Default port 9500. There is no authentication, so use it only on trusted networks.
- **Lightweight:** Python 3.10+. The only runtime dependency is `nvidia-ml-py`; HTTP uses the standard library. Requests read a cached snapshot and never trigger sampling.
- **Install** (runs as a systemd service with `DynamicUser`):

  ```bash
  git clone https://github.com/KoSukeWork/dgx_status_monitor.git
  cd dgx_status_monitor
  sudo bash deploy/install.sh
  curl http://127.0.0.1:9500/api/v1/snapshot
  ```

- **Upgrade:** `git pull && sudo bash deploy/install.sh` (your config in `/etc/default/dgx-status-monitor` is kept).
- Unavailable fields are `null`; every key is always present. Configuration, the field reference, and troubleshooting are documented above in Chinese.

## 许可证

[MIT](LICENSE)
