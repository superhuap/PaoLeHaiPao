#!/usr/bin/env python3
"""多账号跑步记录提交调度器。

    python3 runner.py                 跑全部启用账号
    python3 runner.py cyh lzy         只跑指定账号
    python3 runner.py --check         只体检配置，不发请求
    python3 runner.py --dry-run       按真实时序演练一次，不发请求

账号间隔 start_stagger_seconds 秒依次启动，因此各账号的实时上报彼此错开，
并行运行。单个账号失败不影响其余账号。

依赖：pip install pycryptodome
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import lptiyu as L
import logs

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
ACCOUNTS_DIR = os.path.join(HERE, "accounts")

OK, FAIL, SKIP = "成功", "失败", "跳过"

log = logging.getLogger("runner")

# Ctrl+C 时置位，让 worker 在下一个等待点收敛退出，而不是被硬砍在半路。
STOP = threading.Event()


class Cancelled(Exception):
    """用户在等待途中中断了运行。"""


def say(message: str, level: int = logging.INFO) -> None:
    """全局日志（调度器自身的进展、汇总）。"""
    log.log(level, message)


def note(account: L.Account, message: str,
         level: int = logging.INFO) -> None:
    """账号维度的日志。会同时落到 logs/<账号>.log 与本次运行的副本。"""
    logging.getLogger(f"runner.{account.name}").log(level, message,
                                                   extra={"account": account.name})


# ---------------------------------------------------------------- 配置

@dataclass(frozen=True)
class Config:
    host: str
    oss_endpoint: str
    timeout: int
    distance: float
    used_time: int
    end_margin: int
    segment_ratio: float
    report_count: int
    report_interval: int
    end_pad: int
    submit_tail: int
    renew_face_before_submit: bool
    start_stagger_seconds: int

    @property
    def window(self) -> int:
        return self.used_time + self.end_margin

    @property
    def report_span(self) -> int:
        return (self.report_count - 1) * self.report_interval

    def validate(self) -> None:
        """把「配错了必然失败」的组合挡在发请求之前。

        这几条的错误提示都会伪装成服务端的一个笼统错误，不预检就只能靠猜。
        """
        if self.end_pad >= self.submit_tail:
            raise L.ProtocolError(
                f"end_pad({self.end_pad}) 必须小于 submit_tail({self.submit_tail})，"
                f"否则 end_time 会晚于提交时刻，触发服务端的零延迟拒绝"
                f"（错误表现为「跑步时间异常」，极具误导性）")
        if self.report_span == 0 and self.report_count > 1:
            raise L.ProtocolError(
                f"report_count={self.report_count} 但 report_interval=0，"
                f"上报会全部挤在同一瞬间。实测服务端校验的是上报对窗口的覆盖，"
                f"而不是『有没有』，这样提交必被判「跑步时间异常」")
        if self.report_span and self.report_span >= self.window:
            raise L.ProtocolError(
                f"上报跨度 {self.report_span}s 已覆盖甚至超过整个窗口 "
                f"{self.window}s，end_time 会被推到未来")


def load_config(path: str = CONFIG_PATH) -> Config:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    cfg = Config(
        host=raw.get("host", L.DEFAULT_HOST),
        oss_endpoint=raw.get("oss_endpoint", L.DEFAULT_OSS_ENDPOINT),
        timeout=int(raw.get("timeout", 20)),
        distance=float(raw["distance"]),
        used_time=int(raw["used_time"]),
        end_margin=int(raw["end_margin"]),
        segment_ratio=float(raw["segment_ratio"]),
        report_count=int(raw["report_count"]),
        report_interval=int(raw["report_interval"]),
        end_pad=int(raw["end_pad"]),
        submit_tail=int(raw["submit_tail"]),
        renew_face_before_submit=bool(raw.get("renew_face_before_submit", True)),
        start_stagger_seconds=int(raw.get("start_stagger_seconds", 10)),
    )
    cfg.validate()
    return cfg


# ---------------------------------------------------------------- 时序

@dataclass(frozen=True)
class Timeline:
    t0: int
    start_time: int
    end_time: int
    used_time: int
    margin: int
    end_pad: int
    submit_tail: int

    @property
    def reports_end(self) -> int:
        return self.end_time - self.end_pad

    @property
    def submit_at(self) -> int:
        return self.reports_end + self.submit_tail


def plan(cfg: Config, at: int | None = None) -> Timeline:
    """由上报时刻表反推 start/end/used_time。

    三条硬约束同时成立才能过服务端校验：

      1. 首次上报必须落在窗口**开头附近**。把窗口整体往回拨虽然能免掉等待，
         但必然把上报挤到末端 —— 实测判「跑步时间异常」。
      2. end_time 必须晚于末次上报（否则窗口没盖住上报）。
      3. end_time 必须早于提交时刻，否则触发零延迟拒绝。
    """
    t0 = int(time.time()) if at is None else at
    reports_end = t0 + (cfg.report_count - 1) * cfg.report_interval
    end_time = reports_end + cfg.end_pad
    return Timeline(t0=t0, start_time=end_time - cfg.window,
                    end_time=end_time, used_time=cfg.used_time,
                    margin=cfg.end_margin, end_pad=cfg.end_pad,
                    submit_tail=cfg.submit_tail)


# ---------------------------------------------------------------- 单账号流程

def _session(step: Callable[[], Any], account: L.Account, cfg: Config,
             label: str) -> Any:
    """在 101/102 时自动续期并重试一次。

    续期成功立刻落盘 —— 旧的一对已经作废，返回值丢掉就等于烧掉凭据。
    """
    try:
        return step()
    except L.AuthError:
        if not account.refresh_token:
            raise L.AuthError(
                f"{label} token 失效且账号未配置 refresh_token，无法自动续期；"
                f"请重新登录并在手机上抓取凭据") from None
        identity = account.identity()
        token, refresh = L.renew_token(identity, account.refresh_token,
                                       cfg.host, cfg.timeout)
        L.save_field(account, "token", token)
        L.save_field(account, "refresh_token", refresh)
        account.token, account.refresh_token = token, refresh
        note(account, "鉴权失效 → 已续期并写回", logging.WARNING)
        return step()


def run_account(account: L.Account, cfg: Config, dry_run: bool = False,
                _renewed: bool = False) -> tuple[str, str]:
    """跑完一个账号的完整流程，返回 (结果, 说明)。

    `_renewed` 防止递归二次续期。续期会立刻作废旧的一对，重复调用等于
    连锁作废 —— 新拿到的那对还没落盘就被下一次续期顶掉了，最终什么都没剩下。
    """
    identity = account.identity()
    L.bind_account(account.name)

    try:
        rules = L.fetch_rules(identity, cfg.host, cfg.timeout)
    except L.AuthError:
        if dry_run:
            return FAIL, "token 已失效（演练不做续期这类有副作用的动作）"
        if _renewed:
            return FAIL, ("token 续期后仍失效。可能已用同一对凭据重复续期"
                          "（每次续期都会作废旧的一对），请重新登录并在手机上抓取凭据")
        try:
            renewed = _session_probe(account, cfg)
        except L.AuthError as exc:
            return FAIL, f"续期失败：{exc}；凭据对已耗尽，需重新登录并在手机上抓取"
        return run_account(renewed, cfg, dry_run, _renewed=True)
    except L.ProtocolError as exc:
        return FAIL, str(exc)

    distance = rules.distance_for(cfg.distance)
    if distance != cfg.distance:
        note(account, f"距离被服务端区间收敛为 {distance} km", logging.WARNING)
    tl = plan(cfg)
    points, start_time = L.build_track(rules, distance, tl.used_time,
                                       tl.margin, tl.end_time, cfg.segment_ratio)

    try:
        L.preflight(rules, points, distance, tl.used_time, tl.end_time)
        L.check_window(rules, tl.end_time, tl.used_time, tl.margin)
    except L.PreflightError as exc:
        return FAIL, f"自检未通过：{exc}"
    except L.WindowError as exc:
        return SKIP, str(exc)

    pace = tl.used_time / distance
    note(account, f"{distance} km / {tl.used_time}s / 配速 {pace:.0f}s/km "
                  f"(规则 {rules.pace_lo}–{rules.pace_hi})")
    note(account,
         f"窗口 {time.strftime('%H:%M:%S', time.localtime(tl.start_time))}"
         f" → {time.strftime('%H:%M:%S', time.localtime(tl.end_time))}"
         f"（{cfg.window}s）· 上报 {cfg.report_count}×{cfg.report_interval}s"
         f" · 提交约 {time.strftime('%H:%M:%S', time.localtime(tl.submit_at))}")

    if dry_run:
        return OK, "演练通过，未发任何请求"

    def refresh_face() -> None:
        L.bind_account(account.name)
        new_face = _session(lambda: L.renew_face_id(
            account.identity(), rules, account.face_file or "",
            cfg.host, cfg.timeout), account, cfg, "faceVerify")
        L.save_field(account, "face_verify_id", new_face)
        account.face_verify_id = new_face
        note(account, f"人脸重核完成 {new_face}")

    # 先进实时上报的重活之前做一次。凭据、人脸照片这些出问题的概率远高于
    # 轨迹本身，放在 7 分钟等待之后才验，出错时一次就是 7 分钟。
    # 人脸 id 的有效期远长于一次运行（实测 41 分钟仍可用），所以提前刷新
    # 不会让 id 在提交时过期；提交前那次只是确保新鲜。
    if cfg.renew_face_before_submit:
        try:
            refresh_face()
        except L.ProtocolError as exc:
            return FAIL, f"人脸重核失败：{exc}"

    all_ids = ",".join(str(p.point_id) for p in points)
    try:
        for index in range(cfg.report_count):
            point = points[index % len(points)]
            target = tl.t0 + index * cfg.report_interval
            _wait_until(target)
            _session(lambda: L.report_location(account.identity(), rules, point,
                                               all_ids, cfg.host, cfg.timeout),
                     account, cfg, "setRunLocationRecord")
            note(account,
                 f"上报 {index + 1}/{cfg.report_count} "
                 f"{time.strftime('%H:%M:%S', time.localtime(target))} "
                 f"{point.latitude:.5f},{point.longitude:.5f}", logging.DEBUG)
        _wait_until(tl.submit_at)
    except L.ProtocolError as exc:
        return FAIL, str(exc)

    identity = account.identity()
    try:
        if cfg.renew_face_before_submit:
            refresh_face()

        _session(lambda: L.register_checkpoints(identity, rules, points,
                                                 cfg.host, cfg.timeout),
                 account, cfg, "getStudentVenaPoint")
        sts = _session(lambda: L.fetch_sts(identity, rules, cfg.host, cfg.timeout),
                       account, cfg, "getOssSts")
        key = L.object_key(int(time.time() * 1000))
        L.upload_track(sts, cfg.oss_endpoint, key, json.dumps({
            "uid": account.uid, "game_id": rules.game_id,
            "start_time": tl.start_time, "end_time": tl.end_time,
            "distance": distance, "used_time": tl.used_time,
            "point_list": [p.as_json() for p in points],
        }, ensure_ascii=False, separators=(",", ":")).encode(), cfg.timeout)
    except L.ProtocolError as exc:
        return FAIL, str(exc)

    try:
        record = _session(lambda: L.submit_run(
            account.identity(), rules, key, points, distance, tl.used_time,
            tl.start_time, tl.end_time, cfg.host, cfg.timeout,
            account.face_verify_id or ""),
            account, cfg, "stopRunV278")
    except L.QuotaError as exc:
        return SKIP, f"当日配额已满：{exc}"
    except L.DuplicateError as exc:
        return SKIP, f"重复记录：{exc}"
    except L.RejectedError as exc:
        return FAIL, str(exc)
    except L.ProtocolError as exc:
        return FAIL, str(exc)

    if record.get("record_status") != 1:
        return FAIL, (f"{record.get('record_failed_reason') or '<空>'}"
                      f"（record_id={record.get('record_id')}）")
    return OK, f"record_id={record.get('record_id')}"


def _session_probe(account: L.Account, cfg: Config) -> L.Account:
    """凭据失效时先续期，再由调用方重跑。返回已更新的 account。

    续期后**立刻**写回：旧的一对当刻作废，新的一对若此时丢掉就等于彻底失联。
    """
    if not account.refresh_token:
        raise L.AuthError(f"[{account.name}] token 失效且未配置 refresh_token")
    token, refresh = L.renew_token(account.identity(), account.refresh_token,
                                   cfg.host, cfg.timeout)
    L.save_field(account, "token", token)
    L.save_field(account, "refresh_token", refresh)
    account.token, account.refresh_token = token, refresh
    return account


def _wait_until(target: int) -> None:
    """等到目标时刻。中途收到停止信号则抛 Cancelled，让流程在可控的地方收尾。"""
    while True:
        if STOP.is_set():
            raise Cancelled()
        remaining = target - time.time()
        if remaining <= 0:
            return
        time.sleep(min(2.0, remaining))


# ---------------------------------------------------------------- 调度

def check(account: L.Account, cfg: Config) -> str:
    """只读体检：加载配置、拉一次规则，不产生任何记录。"""
    problems = []
    if not account.refresh_token:
        problems.append("无 refresh_token，token 失效后无法自愈")
    if not account.face_file:
        problems.append("无 face_file，face_verify_id 失效后无法自愈")
    try:
        rules = L.fetch_rules(account.identity(), cfg.host, cfg.timeout)
        pace = cfg.used_time / rules.distance_for(cfg.distance)
        if not rules.pace_lo <= pace <= rules.pace_hi:
            problems.append(
                f"配速 {pace:.0f}s/km 超出规则 {rules.pace_lo}–{rules.pace_hi}")
        if not rules.min_distance <= rules.distance_for(cfg.distance) <= rules.max_distance:
            problems.append("距离超出规则")
    except L.AuthError:
        problems.append("token 已失效")
    except L.ProtocolError as exc:
        problems.append(str(exc))
    return "；".join(problems) if problems else "正常"


def main() -> int:
    parser = argparse.ArgumentParser(description="步道乐跑多账号调度器")
    parser.add_argument("names", nargs="*", help="账号名，省略则跑全部")
    parser.add_argument("--check", action="store_true", help="只体检，不发请求")
    parser.add_argument("--dry-run", action="store_true",
                        help="按真实时序演练，不发请求")
    parser.add_argument("--serial", action="store_true",
                        help="串行执行，不做错峰并行")
    parser.add_argument("--log-level", default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                        help="日志级别；DEBUG 会记录收发报文（已脱敏）")
    parser.add_argument("--quiet", action="store_true",
                        help="控制台只显示警告及以上")
    args = parser.parse_args()

    logs.configure(args.log_level, quiet=args.quiet)
    say(f"runner 启动 pid={os.getpid()} run={logs.run_id()}", logging.DEBUG)

    try:
        cfg = load_config()
    except (OSError, KeyError, ValueError, L.ProtocolError) as exc:
        say(f"配置错误：{exc}", logging.ERROR)
        return 2

    paths = sorted(os.path.join(ACCOUNTS_DIR, n)
                   for n in os.listdir(ACCOUNTS_DIR) if n.endswith(".json"))
    accounts = []
    for path in paths:
        try:
            account = L.load_account(path)
        except (OSError, ValueError, L.ProtocolError) as exc:
            say(f"{os.path.basename(path)} 加载失败：{exc}", logging.ERROR)
            continue
        if args.names and account.name not in args.names:
            continue
        if not account.enabled and not args.names:
            continue
        accounts.append(account)

    if not accounts:
        say("没有可执行的账号", logging.ERROR)
        return 2

    say(f"配置 {CONFIG_PATH}")
    say(f"时序 {cfg.window}s 窗口 · 上报 {cfg.report_count}×{cfg.report_interval}s"
        f" · 每人约 {cfg.report_span + cfg.submit_tail}s")
    say(f"账号 {len(accounts)} 个，间隔 {cfg.start_stagger_seconds}s 启动\n")

    if args.check:
        bad = 0
        for account in accounts:
            result = check(account, cfg)
            bad += result != "正常"
            note(account, f"体检 {result}",
                 logging.INFO if result == "正常" else logging.WARNING)
        return 1 if bad else 0

    if args.dry_run:
        for account in accounts:
            note(account, "开始", logging.DEBUG)
            status, detail = run_account(account, cfg, dry_run=True)
            note(account, f"{status}：{detail}",
                 logging.INFO if status != FAIL else logging.ERROR)
        return 0

    started = time.time()
    results: dict[str, tuple[str, str]] = {}

    def worker(account: L.Account) -> None:
        started_at = time.time()
        note(account, "开始", logging.INFO)
        try:
            results[account.name] = run_account(account, cfg)
        except Cancelled:
            results[account.name] = (SKIP, "已被中断")
        except Exception as exc:                      # noqa: BLE001
            results[account.name] = (FAIL, f"{type(exc).__name__}: {exc}")
        finally:
            L.bind_account(None)
            # 立刻落结果。整体流程要跑 7 分钟，等到最后汇总才出结论的话，
            # 中途的失败会长时间没有下文。
            status, detail = results.get(account.name, (FAIL, "未执行"))
            note(account, f"{status}  {detail}（耗时 {time.time() - started_at:.0f}s）",
                 logging.INFO if status == OK
                 else logging.WARNING if status == SKIP else logging.ERROR)

    try:
        if args.serial:
            for account in accounts:
                worker(account)
        else:
            threads = []
            for index, account in enumerate(accounts):
                if index:
                    # 错峰在主线程做：启动间隔由配置控制，与各账号自身
                    # 7 分钟的等待无关。
                    time.sleep(cfg.start_stagger_seconds)
                thread = threading.Thread(target=worker, args=(account,),
                                         daemon=True)
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()
    except KeyboardInterrupt:
        STOP.set()
        say("收到中断，正在让各账号收敛退出…", logging.WARNING)
        if not args.serial:
            for thread in threading.enumerate():
                if thread is not threading.current_thread():
                    thread.join(timeout=5)
        say("已中断", logging.WARNING)
        for account in accounts:
            results.setdefault(account.name, (SKIP, "已被中断"))
        return 130

    say("=" * 60)
    tally = {OK: 0, FAIL: 0, SKIP: 0}
    for account in accounts:
        status, detail = results.get(account.name, (FAIL, "未执行"))
        tally[status] = tally.get(status, 0) + 1
        note(account, f"{status}  {detail}",
                 logging.INFO if status == OK else logging.WARNING)
    say(f"合计  成功 {tally[OK]} · 失败 {tally[FAIL]} · 跳过 {tally[SKIP]}")

    say(f"耗时 {time.time() - started:.0f}s", logging.DEBUG)
    return 1 if tally[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())