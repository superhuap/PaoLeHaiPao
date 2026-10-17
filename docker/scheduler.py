#!/usr/bin/env python3
"""容器内的常驻调度器：每天在固定时刻跑一轮 runner.py。

时区固定按 TZ（默认 Asia/Shanghai，即 UTC+8）计算，与宿主机时区无关。
一次跑不完就等它跑完，绝不并发叠两轮 —— runner.py 自己会按
start_stagger_seconds 错峰并行，多轮并行只会互相踩状态。

    SCHEDULE_TIMES=07:30,13:30   每天触发时刻（24 小时制，逗号分隔）
    RUN_ARGS=--serial            透传给 runner.py 的额外参数
    RUN_ON_START=1               启动时先立刻跑一轮（首次部署建议开）
    DATA_DIR=/data               数据目录，账号 JSON 与日志都在这

日志：控制台（即 docker logs）与 $DATA_DIR/logs/run-YYYY-MM-DD.log 同时一份，
logs.py 本身只往 stderr 写，落盘由这里负责。
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone, tzinfo

DATA_DIR = os.environ.get("DATA_DIR", "/data")
RUNNER = os.environ.get("RUNNER", "/app/runner.py")
LOG_DIR = os.path.join(DATA_DIR, "logs")
STATE_DIR = os.path.join(DATA_DIR, "state")
HEARTBEAT = os.path.join(STATE_DIR, "heartbeat")
HEARTBEAT_EVERY = 15.0
# 睡眠切片要小：信号处理器返回后 time.sleep 会接着睡完剩余时间
#（PEP 475），切 5s 是为了 docker stop 的响应延迟不超 5s。
SLEEP_CHUNK = 5.0
DEFAULT_TIMES = "07:30,13:30"

_stopping = False
_running: subprocess.Popen[str] | None = None


# ---------------------------------------------------------------- 时区 / 时刻

def resolve_tz() -> tzinfo:
    """TZ 环境变量优先；拿不到就退到固定 UTC+8，保证时刻不漂。"""
    name = os.environ.get("TZ") or "Asia/Shanghai"
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        try:
            from zoneinfo import ZoneInfo

            fallback = ZoneInfo("Asia/Shanghai")
            log(f"时区 {name} 不可用，回退 Asia/Shanghai")
            return fallback
        except Exception:
            log("zoneinfo 不可用，退到固定 UTC+8")
            return timezone(timedelta(hours=8))


def log(message: str) -> None:
    stamp = datetime.now(CLOCK).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[sched {stamp}] {message}", flush=True)


# 放在 log 之后：resolve_tz 兜底分支要打日志
CLOCK = resolve_tz()


def parse_times(raw: str) -> list[tuple[int, int]]:
    times: list[tuple[int, int]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 2:
            raise ValueError(f"时刻格式应为 HH:MM，收到 {chunk!r}")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"时刻越界：{chunk!r}")
        times.append((hour, minute))
    if not times:
        raise ValueError("SCHEDULE_TIMES 为空")
    return sorted(set(times))


def next_run(after: datetime, times: list[tuple[int, int]]) -> datetime:
    """严格晚于 after 的下一个触发时刻（含跨天）。"""
    candidates = []
    for offset in (0, 1):
        day = (after + timedelta(days=offset)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        for hour, minute in times:
            moment = day + timedelta(hours=hour, minutes=minute)
            if moment > after:
                candidates.append(moment)
    if not candidates:  # pragma: no cover - 只有在 times 非空时不可能发生
        raise RuntimeError("找不到下一个触发时刻")
    return min(candidates)


# ---------------------------------------------------------------- 文件属主

def detect_owner() -> tuple[int, int] | None:
    """数据卷里若有非 root 属主的文件就记下来。

    容器以 root 跑时（Linux 上 bind mount 的常见情形），runner.py 原子写出的
    新文件会变成 root:root + 0600，宿主机上就再也改不动了。这里先探一次，
    每轮跑完再把这些新文件还回去。命名卷（纯 root 属主）探不到，就不动。
    """
    if os.geteuid() != 0:
        return None
    try:
        for name in sorted(os.listdir(DATA_DIR)):
            path = os.path.join(DATA_DIR, name)
            stat = os.stat(path)
            if stat.st_uid != 0:
                return stat.st_uid, stat.st_gid
    except OSError:
        return None
    return None


def restore_owner(owner: tuple[int, int] | None) -> None:
    if owner is None:
        return
    uid, gid = owner
    try:
        entries = [(DATA_DIR, name) for name in os.listdir(DATA_DIR)]
        entries += [(os.path.join(DATA_DIR, "accounts"), name)
                    for name in os.listdir(os.path.join(DATA_DIR, "accounts"))]
    except OSError:
        return
    for base, name in entries:
        path = os.path.join(base, name)
        try:
            stat = os.stat(path)
            if (stat.st_uid, stat.st_gid) != (uid, gid):
                os.chown(path, uid, gid)
        except OSError:
            continue


# ---------------------------------------------------------------- 执行一轮

def extra_args() -> list[str]:
    return shlex.split(os.environ.get("RUN_ARGS", "").strip())


def run_once(reason: str, owner: tuple[int, int] | None) -> int:
    """跑一轮 runner.py，输出同时进控制台和当天日志文件。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    day = datetime.now(CLOCK).strftime("%Y-%m-%d")
    logfile = os.path.join(LOG_DIR, f"run-{day}.log")
    started = time.time()
    log(f"开始（{reason}）→ {logfile}")

    global _running
    with open(logfile, "a", encoding="utf-8", buffering=1) as handle:
        handle.write(f"\n===== {datetime.now(CLOCK).isoformat(timespec='seconds')} "
                     f"{reason} =====\n")
        try:
            process = subprocess.Popen(
                [sys.executable, RUNNER, *extra_args()],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as exc:
            log(f"拉起 runner.py 失败：{exc}")
            return 2
        _running = process

        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            handle.write(line)
        code = process.wait()
        _running = None

    elapsed = int(time.time() - started)
    restore_owner(owner)
    if _stopping:
        log(f"已中止（{reason}），耗时 {elapsed}s")
    else:
        log(f"结束（{reason}）退出码 {code}，耗时 {elapsed}s")
    return code


# ---------------------------------------------------------------- 主循环

def heartbeat() -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(HEARTBEAT, "w", encoding="utf-8") as handle:
            handle.write(datetime.now(CLOCK).isoformat(timespec="seconds"))
    except OSError:
        pass


def sleep_until(moment: datetime) -> None:
    """分段睡，每段顺手续心跳 —— healthcheck 靠它判断调度器还活着。"""
    beat = time.time()
    while not _stopping:
        remain = (moment - datetime.now(moment.tzinfo)).total_seconds()
        if remain <= 0:
            return
        time.sleep(min(remain, SLEEP_CHUNK))
        if time.time() - beat >= HEARTBEAT_EVERY:
            heartbeat()
            beat = time.time()


def handle_signal(signum: int, _frame: object) -> None:
    """停止信号要传给 runner.py，否则 docker stop 会干等一整轮跑完。"""
    global _stopping
    _stopping = True
    log(f"收到信号 {signal.Signals(signum).name}，准备退出")
    if _running is not None and _running.poll() is None:
        try:
            _running.terminate()
        except OSError:
            pass


def main() -> int:
    times = parse_times(os.environ.get("SCHEDULE_TIMES", DEFAULT_TIMES))
    owner = detect_owner()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, handle_signal)

    heartbeat()
    labels = "、".join(f"{h:02d}:{m:02d}" for h, m in times)
    log(f"时区 {getattr(CLOCK, 'key', 'UTC+8')} · 每天 {labels} 触发 · "
        f"runner {RUNNER} · 附加参数 {shlex.join(extra_args()) or '无'}")

    if os.environ.get("RUN_ON_START", "0").strip() in ("1", "true", "yes", "on"):
        run_once("启动即跑 RUN_ON_START", owner)
        if _stopping:
            return 0

    while not _stopping:
        moment = next_run(datetime.now(CLOCK), times)
        wait = max(0.0, (moment - datetime.now(CLOCK)).total_seconds())
        log(f"下次触发 {moment:%Y-%m-%d %H:%M}（{wait / 3600:.2f}h 后）")
        sleep_until(moment)
        if _stopping:
            break
        run_once(f"定时 {moment:%H:%M}", owner)

    log("调度器退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())