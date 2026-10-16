"""步道乐跑跑步记录提交器 —— 协议层。

一个账号一次提交，三步流程：

    beforeRunV260  →  getOssSts  →  OSS 表单上传  →  stopRunV278

提交前的两件事是服务端强制要求，缺一即被拒（均经实测确认）：

    1. 跑步窗口内铺满 setRunLocationRecord 实时上报 —— 缺失判「跑步时间异常」
    2. 走完 faceVerify 的 type=0 + type=2 确认 —— 不完整判「人脸验证不合格」

其余踩过的坑都在对应函数处标注了原因，不要随手"简化"。
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import logging
import math
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# ---------------------------------------------------------------- 协议常量

API_KEY = b"Wet2C8d34f62ndi3"
API_IV = b"K6iv85jBD8jgf32D"
SIGN_SALT = "rDJiNB9j7vD2"
BLOCK = 16
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49(0x18003128) "
    "NetType/WIFI Language/zh_CN miniProgram"
)

BEFORE_API = "/v3/api.php/Run2/beforeRunV260"
STS_API = "/v3/api.php/WpIndex/getOssSts"
STOP_API = "/v3/api.php/Run/stopRunV278"
REFRESH_API = "/v3/api.php/Login/RefreshToken"
FACE_API = "/v3/api.php/Run2/faceVerify"
VENA_API = "/v3/api.php/Run2/getStudentVenaPoint"
LIVE_API = "/v3/api.php/Run/setRunLocationRecord"

DEFAULT_HOST = "https://api2.lptiyu.com"
DEFAULT_OSS_ENDPOINT = "https://lptiyu-data.oss-cn-hangzhou.aliyuncs.com"

log = logging.getLogger("runner")
_current_account: dict[str, str] = {}


def _EXTRA() -> dict[str, str]:
    """当前账号维度。多账号并行时靠调用方切���。"""
    return {"account": _current_account.get("name", "调度")}


def bind_account(name: str | None) -> None:
    """声明当前代码属于哪个账号，使协议层日志在控制台上带账号维度。"""
    if name is None:
        _current_account.pop("name", None)
    else:
        _current_account["name"] = name


def _peek(blob: str) -> Any:
    """把信封解开取明文，只为日志可读性；解不开就原样返回。"""
    try:
        return decrypt(blob, API_KEY, API_IV)
    except Exception:                                     # noqa: BLE001
        return blob[:120]


def _trace(direction: str, path: str, payload: Any) -> None:
    """DEBUG 级报文追踪。

    记录的是**解密后的明文**而非密文 —— 密文既不可读，脱敏规则（按 JSON 字段名
    匹配）也作用不上。凭据在写入日志前替换成首尾片段。
    """
    if not log.isEnabledFor(logging.DEBUG):
        return
    from logs import mask                      # 延迟导入，避免模块级循环依赖
    text = payload if isinstance(payload, str) \
        else json.dumps(payload, ensure_ascii=False)
    text = mask(text)
    if len(text) > 600:
        text = text[:600] + f"…(共 {len(text)} 字符)"
    log.debug("%s %s %s", direction, path, text, extra=_EXTRA())


# ---------------------------------------------------------------- 异常

class ProtocolError(Exception):
    """流程无法完成。"""


class AuthError(ProtocolError):
    """token 失效，且续期未恢复。"""


class TransportError(ProtocolError):
    """网络或服务端返回了无法理解的响应。"""


class QuotaError(ProtocolError):
    """当日关联成绩次数已达上限。"""


class DuplicateError(ProtocolError):
    """当日同一时段已有记录。"""


class WindowError(ProtocolError):
    """不在可提交时段。"""


class PreflightError(ProtocolError):
    """本地规则自检未通过。"""


class RejectedError(ProtocolError):
    """服务端创建了记录但判为无效。"""


# ---------------------------------------------------------------- 密码学

def sign(params: dict[str, Any]) -> str:
    """内层签名：按键名排序后键值直接相连，公开盐追加在**末尾**，取 MD5。

    形式上极易写错，务必注意：不要加等号，盐只在结尾而非首尾各一。
    跑步层用的是另一把私有盐，不走这里。
    """
    joined = "".join(f"{k}{params[k]}" for k in sorted(params))
    return hashlib.md5((joined + SIGN_SALT).encode()).hexdigest()


def encrypt(text: str, key: bytes, iv: bytes) -> str:
    return base64.b64encode(
        AES.new(key, AES.MODE_CBC, iv).encrypt(pad(text.encode(), BLOCK))).decode()


def decrypt(blob: str, key: bytes, iv: bytes) -> Any:
    text = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(
        base64.b64decode(blob)), BLOCK).decode("utf-8")
    return json.loads(text.replace(r"\/", "/"))


def envelope(params: dict[str, Any]) -> dict[str, str]:
    """小程序信封。鉴权中间件按请求格式而非语义放行，这层只校验公开盐签名。"""
    body = dict(params)
    body["sign"] = sign(body)
    return {"ostype": "5", "data": encrypt(
        json.dumps(body, separators=(",", ":")), API_KEY, API_IV)}


# ---------------------------------------------------------------- 传输

def _post(path: str, form: dict[str, str], host: str, timeout: int) -> dict[str, Any]:
    _trace("→", path.split("/")[-1], _peek(form["data"]))
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "User-Agent": UA,
        "Accept-Encoding": "gzip",
        "Host": urllib.parse.urlparse(host).netloc,
    }
    request = urllib.request.Request(host + path, method="POST",
                                     data=urllib.parse.urlencode(form).encode(),
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransportError(f"{type(exc).__name__}: {exc}") from exc

    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    try:
        result = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TransportError(f"响应非 JSON（HTTP {status}，{len(raw)} 字节）") from exc
    if not isinstance(result, dict):
        raise TransportError("响应不是 JSON 对象")
    _trace("←", path.split("/")[-1],
           _peek(result["data"]) if isinstance(result.get("data"), str)
           else {k: v for k, v in result.items() if k != "data"})
    return result


def interpret(reply: dict[str, Any]) -> dict[str, Any]:
    """取出 data 里的业务数据，并把可识别的失败归类。

    注意错误消息的伪装：end_time 落在未来（零延迟）与缺少实时上报，都会返回
    「跑步时间异常」，但成因完全不同，修错方向也就完全不同。
    """
    code = reply.get("status")
    info = str(reply.get("info") or "")
    if code in (101, 102):
        raise AuthError(info or f"status={code}")
    if "跑步记录已存在" in info:
        raise DuplicateError(info)
    if "关联成绩次数" in info:
        raise QuotaError(info)
    data = reply.get("data")
    if isinstance(data, str):
        return decrypt(data, API_KEY, API_IV)
    if isinstance(data, dict):
        # 少数接口（Run2/faceVerify 等）直接回明文对象而非加密串
        return data
    raise TransportError(f"未返回 data：status={code} info={info or '<空>'}")


def call(path: str, params: dict[str, Any], host: str, timeout: int) -> dict[str, Any]:
    return interpret(_post(path, envelope(params), host, timeout))


def _stamp() -> tuple[int, str]:
    return int(time.time()), str(random.randint(100000, 999999))


# ---------------------------------------------------------------- 规则

@dataclass(frozen=True)
class Checkpoint:
    id: int
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Rules:
    """beforeRunV260 下发的判分规则与身份三元组。

    一律现取，不写死。服务端改规则后硬编码的脚本会静默失效，而排查时无从
    判断是规则变了还是参数错了 —— 这个坑已经踩过一次。
    """
    school_id: str
    term_id: str
    game_id: str
    checkpoints: tuple[Checkpoint, ...]
    min_distance: float
    max_distance: float
    pace_lo: int
    pace_hi: int
    min_log_num: int
    window_start: int
    window_end: int
    point_max_gap: float

    def distance_for(self, wanted: float) -> float:
        return max(self.min_distance, min(wanted, self.max_distance))


def parse_rules(payload: dict[str, Any]) -> Rules:
    try:
        rule = next(r for r in payload["time_rule_arr"]
                    if str(r.get("time_type", "3")) == "3")
    except (KeyError, StopIteration) as exc:
        raise TransportError("beforeRunV260 未下发 time_rule_arr") from exc

    line = payload.get("run_line_info") or {}
    raw_points = line.get("point_list") or []
    if not raw_points:
        raise TransportError("beforeRunV260 未下发打卡点列表")
    checkpoints = tuple(sorted(
        (Checkpoint(int(p["id"]),
                    float(str(p["jingwei"]).split(",")[0]),
                    float(str(p["jingwei"]).split(",")[1]))
         for p in raw_points),
        key=lambda c: c.id))

    # pace 字段命名反直觉：min_pace=540 是慢的一端，max_pace=180 是快的一端。
    pace_lo, pace_hi = sorted((int(rule["max_pace"]), int(rule["min_pace"])))

    return Rules(
        school_id=str(payload["school_id"]),
        term_id=str(payload["term_id"]),
        game_id=str(payload["game_id"]),
        checkpoints=checkpoints,
        min_distance=float(rule["min_distance"]),
        max_distance=float(rule["max_distance"]),
        pace_lo=pace_lo,
        pace_hi=pace_hi,
        min_log_num=int(rule.get("min_log_num", 4)),
        window_start=int(rule["start_time"]),
        window_end=int(rule["end_time"]),
        point_max_gap=float(line.get("point_max_distance2", 2.0)),
    )


def fetch_rules(identity: dict[str, Any], host: str, timeout: int) -> Rules:
    """拉取判分规则。

    只需 token + uid + student_num 即可调用，不需要先有 term_id / game_id；
    请求里写错的 term_id 会被服务端忽略并返回当前生效值。这也是账号 JSON
    不必写 term_id 的原因 —— 换学期时不用改文件。
    """
    stamp, nonce = _stamp()
    return parse_rules(call(BEFORE_API, {
        **identity,
        "token": identity["token"],
        "access_token": identity["token"],
        "uid": identity["uid"],
        "student_num": identity["student_num"],
        "ostype": 5, "timestamp": stamp, "nonce": nonce,
    }, host, timeout))


# ---------------------------------------------------------------- 账号

@dataclass
class Account:
    name: str
    token: str
    uid: str
    student_num: str
    refresh_token: str | None = None
    face_verify_id: str | None = None
    face_file: str | None = None
    source: str | None = None
    enabled: bool = True
    max_runs_per_day: int = 2

    @property
    def fingerprint(self) -> dict[str, Any]:
        """设备指纹。与凭据无关，但服务端会拿它核对请求来源。"""
        return {
            "buildVersion": 2,
            "mobileDeviceId": "A6CC209A-6FE4-423D-956D-D68CCF69E42D",
            "mobileModel": "iPhone14,5",
            "mobileOsVersion": "iOS18.7.7",
            "version": "4.3.0",
        }

    def identity(self, token: str | None = None) -> dict[str, Any]:
        """设备指纹 + 身份 + token，展开进请求体即可。

        school_id / term_id / game_id **不在**这里 —— 它们由 beforeRunV260
        下发，随规则对象走，需要时显式传 rules。account.py 里有一个自检会
        校验本函数提供的字段覆盖了全部 `identity[...]` 读取，避免漏字段时
        要等跑满一轮（7 分钟）才炸 KeyError。
        """
        return {**self.fingerprint, "uid": self.uid,
                "student_num": self.student_num, "token": token or self.token}


def load_account(path: str) -> Account:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    known = {"name", "token", "refresh_token", "uid", "student_num",
             "face_verify_id", "face_file", "enabled", "max_runs_per_day"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ProtocolError(f"{path}: 未知字段 {'、'.join(unknown)}")
    missing = [k for k in ("token", "uid", "student_num") if not raw.get(k)]
    if missing:
        raise ProtocolError(f"{path}: 缺少 {'、'.join(missing)}")
    return Account(
        name=raw.get("name") or os.path.splitext(os.path.basename(path))[0],
        token=str(raw["token"]),
        uid=str(raw["uid"]),
        student_num=str(raw["student_num"]),
        refresh_token=raw.get("refresh_token"),
        face_verify_id=raw.get("face_verify_id"),
        face_file=raw.get("face_file"),
        source=path,
        enabled=bool(raw.get("enabled", True)),
        max_runs_per_day=int(raw.get("max_runs_per_day", 2)),
    )


# ---------------------------------------------------------------- 凭据

def renew_token(identity: dict[str, Any], refresh_token: str,
                host: str, timeout: int) -> tuple[str, str]:
    """换一对新 token。三条实测约束：

    1. 要求 (access_token, refresh_token) 是**当前活跃的那一对**，续期后旧的
       立即作废，重复使用旧对得到 101。
    2. 返回值必须立刻落盘。丢弃等于烧掉凭据，只能回真机重新登录。
    3. 签发的 token 与请求通道绑定：经信封续期得到的 token 只能用于信封请求。
    """
    stamp, nonce = _stamp()
    data = call(REFRESH_API, {
        **identity,
        "access_token": identity["token"],
        "token": identity["token"],
        "refresh_token": refresh_token,
        "uid": identity["uid"],
        "ostype": 5, "timestamp": stamp, "nonce": nonce,
    }, host, timeout)
    token = data.get("access_token")
    if not token:
        raise AuthError("续期响应缺少 access_token")
    return str(token), str(data.get("refresh_token") or refresh_token)


def renew_face_id(identity: dict[str, Any], rules: Rules, face_file: str,
                  host: str, timeout: int, confirms: int = 2) -> str:
    """重做整套人脸核验，返回新的 run_face_verify_id。

    真机上是拍照 + 活体检测，分三步：type=0 取 id，再 type=2 确认两次
    （beforeRunV260 的 middle_num=2）。实测这三步在小程序信封下都能直接调用，
    **不需要 lp31、不需要跑步层私有盐** —— 只要给一个此前成功核验时上传过的
    face_file 路径。

    只做 type=0 拿到的 id 提交会被判「人脸验证不合格」；type=2 必须带
    face_file，否则报「参数异常」。

    school_id 取自 rules 而非 identity —— 它由 beforeRunV260 下发，不在账号里。
    """
    if not face_file:
        raise AuthError(
            "需要重新人脸核验，但账号里没有 face_file。请把真机成功核验时上传的"
            "照片路径（Public/Upload/pic/face_verify/…）填进账号 JSON")

    def ask(**extra: Any) -> str:
        stamp, nonce = _stamp()
        data = call(FACE_API, {
            **identity,
            "token": identity["token"],
            "uid": identity["uid"],
            "student_num": identity["student_num"],
            "school_id": rules.school_id,
            "face_file": face_file,
            "ostype": 5, "timestamp": stamp, "nonce": nonce,
            **extra,
        }, host, timeout)
        return str(data.get("run_face_verify_id") or "")

    face_id = ask(type=0)
    if not face_id:
        raise AuthError("type=0 人脸核验未返回 run_face_verify_id")
    log.info("人脸核验 type=0 通过，id 前缀 %s", face_id[:4],
             extra=_EXTRA())
    for _ in range(max(0, confirms)):
        ask(type=2, run_face_verify_id=face_id)
    log.info("人脸核验 type=2 确认 %d 次完成", max(0, confirms),
             extra=_EXTRA())
    return face_id


def save_field(account: Account, field_name: str, value: str) -> None:
    """把续期出来的新值写回账号 JSON。

    token 和 face_verify_id 都会被服务端作废，都属于需要持久化的状态。
    写入用临时文件 + os.replace 保证原子性；replace 失败时暂存文件里存着
    可用副本，**必须保留**并报错告知位置 —— 它是唯一的恢复路径，删掉等于
    把凭据或人脸 id 永久销毁。
    """
    if not account.source or not value:
        return
    with open(account.source, encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get(field_name) == value:
        return
    data[field_name] = value
    staged = f"{account.source}.tmp"
    try:
        with open(staged, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(staged, 0o600)
        os.replace(staged, account.source)
    except OSError as exc:
        raise ProtocolError(
            f"{exc}；新值已保留在 {staged}，请手动替换到 {account.source}") from exc


# ---------------------------------------------------------------- 轨迹

@dataclass(frozen=True)
class Point:
    distance: float
    latitude: float
    longitude: float
    point_id: int
    time: int

    def as_json(self) -> dict[str, Any]:
        return {"distance": self.distance, "latitude": self.latitude,
                "longtitude": self.longitude, "point_id": self.point_id,
                "time": self.time}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def build_track(rules: Rules, distance: float, used_time: int, margin: int,
                end_time: int, ratio: float) -> tuple[list[Point], int]:
    """构造落点序列，返回 (点位, start_time)。

    log_data[].distance 必须**各点同值**。服务端按「相邻点距离差 ÷ 时间差」
    做按段测速；填累计里程会算出 20 km/h 这类不可能的瞬时速度而被拒，各点
    同值时相邻差为 0，服务端算不出速度。抓包里的有效记录本就如此。

    落点时刻按固定比例铺在 [start, end) 内：首点 20%，末点 88%，末点必须
    严格早于 end_time。
    """
    start_time = end_time - used_time - margin
    segment = round(distance * ratio, 2)
    span = len(rules.checkpoints) - 1
    points = []
    for index, spot in enumerate(rules.checkpoints):
        offset = 0.20 + index * 0.68 / span
        points.append(Point(
            distance=segment,
            latitude=round(spot.latitude, 6),
            longitude=round(spot.longitude, 6),
            point_id=spot.id,
            time=start_time + int(round(used_time * offset)),
        ))
    return points, start_time


def preflight(rules: Rules, points: list[Point], distance: float,
              used_time: int, end_time: int) -> None:
    """比对服务端阈值，任一不满足即中止，不发起请求。"""
    if not rules.min_distance <= distance <= rules.max_distance:
        raise PreflightError(
            f"距离 {distance} km 超出规则 {rules.min_distance}–{rules.max_distance}")
    if len(points) < rules.min_log_num:
        raise PreflightError(f"点位 {len(points)} 个少于规则下限 {rules.min_log_num}")
    pace = used_time / distance
    if not rules.pace_lo <= pace <= rules.pace_hi:
        raise PreflightError(
            f"配速 {pace:.0f} 秒/km 超出规则 {rules.pace_lo}–{rules.pace_hi}")
    if points[-1].time >= end_time:
        raise PreflightError("末个 GPS 点时间必须严格早于 end_time")
    for prev, cur in zip(points, points[1:]):
        gap = haversine_km(prev.latitude, prev.longitude, cur.latitude, cur.longitude)
        if gap > rules.point_max_gap:
            raise PreflightError(f"点间距 {gap:.2f} km 超过围栏上限 {rules.point_max_gap}")


def check_window(rules: Rules, end_time: int, used_time: int, margin: int) -> None:
    """服务端只认当天有效时段内上传的成绩，窗口外直接失败而不回填时间。"""
    day = time.localtime(end_time)
    midnight = int(time.mktime(
        (day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1)))
    at = lambda sec: f"{sec // 3600:02d}:{sec % 3600 // 60:02d}"  # noqa: E731
    if end_time >= midnight + rules.window_end:
        raise WindowError(f"当前 {time.strftime('%H:%M:%S')} 已过当天 "
                          f"{at(rules.window_end)}，今日不可提交")
    if end_time - used_time - margin < midnight + rules.window_start:
        raise WindowError(f"距 {at(rules.window_start)} 不足 {used_time + margin} 秒，"
                          f"今日剩余时间不够完成一次跑步")


# ---------------------------------------------------------------- 反作弊自证

def build_run_check_res(identity: dict[str, Any], distance: float,
                        used_time: int) -> list[dict[str, Any]]:
    """构造 stopRun 的 run_check_res。

    客户端把**自己测到的**结果回传给服务端，一条检查计划对应一个条目：

        plan 1  每 30 秒的实测配速，格式 "0-<km/h>"
        plan 4  每 100m 的漂移，全 0
        plan 5  每 100m 的步数，格式 "<步数>-0"
        plan 6  每 100m 的步数漂移，全 0

    条数按 used_time // 30 取。**与实际上报次数无关** —— 真机样本 used_time=575
    下发 19 条却只上报 4 次，也照样通过。is_cheat / is_stop 恒为 0。
    """
    count = max(1, used_time // 30)
    speed_kmh = distance / used_time * 3600.0
    zeros = ",".join(["0"] * count)
    speeds = ",".join(
        f"0-{max(0.1, speed_kmh * random.uniform(0.94, 1.06)):.2f}" for _ in range(count))
    steps = ",".join(f"{random.randint(50, 70)}-0" for _ in range(count))
    values = {1: speeds, 4: zeros, 5: steps, 6: zeros}
    return [{"plan": plan, "uid": identity["uid"], "inteval_value": values[plan],
             "inteval_result": zeros, "is_cheat": 0, "is_stop": 0}
            for plan in (1, 4, 5, 6)]


# ---------------------------------------------------------------- 三步

def register_checkpoints(identity: dict[str, Any], rules: Rules,
                         points: list[Point], host: str, timeout: int) -> None:
    """Run2/getStudentVenaPoint —— 登记本次要经过的打卡点，响应无业务数据。"""
    order = [str(p.point_id) for p in points]
    random.shuffle(order)
    stamp, nonce = _stamp()
    interpret(_post(VENA_API, envelope({
        **identity, "token": identity["token"],
        "uid": identity["uid"], "student_num": identity["student_num"],
        "school_id": rules.school_id, "game_id": rules.game_id,
        "point_info": json.dumps(order, separators=(",", ":")),
        "ostype": 5, "timestamp": stamp, "nonce": nonce,
    }), host, timeout))


def report_location(identity: dict[str, Any], rules: Rules, point: Point,
                    all_ids: str, host: str, timeout: int) -> None:
    """Run/setRunLocationRecord —— 单次实时位置上报。

    服务端按**请求到达的真实时刻**记录，无法回拨。所以上报必须真实等待，
    也正因如此跑步窗口不能整体往回拨 —— 那样会把上报全挤到窗口末端，
    实测判「跑步时间异常」。上报要从窗口开头就铺开。

    该接口在小程序信封下可直接调用，不需要 lp31。
    """
    stamp, nonce = _stamp()
    interpret(_post(LIVE_API, envelope({
        **identity, "token": identity["token"],
        "uid": identity["uid"], "student_num": identity["student_num"],
        "school_id": rules.school_id, "game_id": rules.game_id,
        "latitude": f"{point.latitude:.6f}", "longitude": f"{point.longitude:.6f}",
        "point_ids": all_ids,
        "ostype": 5, "timestamp": stamp, "nonce": nonce,
    }), host, timeout))


def fetch_sts(identity: dict[str, Any], rules: Rules,
              host: str, timeout: int) -> dict[str, Any]:
    stamp, nonce = _stamp()
    data = call(STS_API, {
        **identity, "token": identity["token"],
        "uid": identity["uid"], "student_num": identity["student_num"],
        "school_id": rules.school_id, "game_id": rules.game_id,
        "ostype": 5, "timestamp": stamp, "nonce": nonce,
    }, host, timeout)
    if "AccessKeyId" not in data:
        raise TransportError("getOssSts 未返回 OSS 临时凭证")
    return data


def upload_track(sts: dict[str, Any], endpoint: str, key: str,
                 body: bytes, timeout: int) -> None:
    """OSS 表单上传。

    必须 POST 到 bucket 根路径、object key 放在表单字段里；PUT 到对象路径
    会被拒（`AccessDenied ... because of bucket acl`）—— 临时密钥只授了表单
    上传权限。成功返回 **204**，不是 200。
    """
    policy = base64.b64encode(json.dumps({
        "expiration": (datetime.now(timezone.utc) + timedelta(seconds=3600))
        .strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "conditions": [["content-length-range", 0, 1 << 30]],
    }, separators=(",", ":")).encode()).decode()
    signature = base64.b64encode(hmac.new(
        sts["AccessKeySecret"].encode(), policy.encode(), hashlib.sha1).digest()).decode()

    boundary = f"----RunBoundary{uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in (("key", key), ("policy", policy),
                        ("OSSAccessKeyId", sts["AccessKeyId"]),
                        ("signature", signature),
                        ("x-oss-security-token", sts["SecurityToken"])):
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(b'Content-Disposition: form-data; name="file"; '
                  b'filename="run_record.txt"\r\nContent-Type: text/plain\r\n\r\n')
    chunks.append(body)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    payload = b"".join(chunks)

    request = urllib.request.Request(endpoint + "/", data=payload, method="POST",
                                     headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(payload)),
        "User-Agent": "aliyun-sdk-js/6.18.0(Chrome/120 Safari/537.36)",
        "Host": urllib.parse.urlparse(endpoint).netloc,
        "Origin": "https://servicewechat.com",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw, status = response.read(), response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransportError(f"OSS 上传失败 {type(exc).__name__}: {exc}") from exc
    log.debug("→ OSS 表单上传 key=%s %d 字节 → HTTP %s",
              key, len(payload), status, extra=_EXTRA())
    if status not in (200, 204):
        raise TransportError(f"OSS 上传失败 HTTP {status}: {raw[:200]!r}")


def object_key(moment: int) -> str:
    return (f"Public/Upload/file/run_record/{datetime.now():%Y-%m-%d}/"
            f"{str(moment)[-3:]}/{moment}-{random.randint(0, 149)}.txt")


def submit_run(identity: dict[str, Any], rules: Rules, key: str, points: list[Point],
               distance: float, used_time: int, start_time: int, end_time: int,
               host: str, timeout: int, face_verify_id: str) -> dict[str, Any]:
    """提交成绩。face_verify_id 显式传入而非走 identity ——

    它只有这一个消费点，混进 identity 会被 ``**identity`` 展开到每一个请求体，
    变成真机不发的多余字段（服务端确实会忽略，但保持一致更利于对拍）。
    """
    stamp, nonce = _stamp()
    return call(STOP_API, {
        **identity, "version_name": "4.3.0",
        "uid": identity["uid"], "token": identity["token"],
        "school_id": rules.school_id, "term_id": rules.term_id,
        "course_id": 0, "class_id": 0, "student_num": identity["student_num"],
        "card_id": "", "timestamp": stamp, "nonce": nonce, "ostype": 5,
        "game_id": rules.game_id,
        "start_time": str(start_time), "end_time": str(end_time),
        # 这四个字段必须是字符串。传 float 会被判「非法请求」，在进入业务逻辑
        # 之前就被废掉，而不是给出可定位的类型错误。
        "distance": f"{distance:.2f}", "used_time": str(used_time),
        "record_img": "", "file_img": "", "is_running_area_valid": 1,
        "step_info": json.dumps({"interval": 60,
                                 "list": [0] * ((end_time - start_time) // 60 + 1)},
                                separators=(",", ":")),
        "step_num": 0,
        "log_data": json.dumps([p.as_json() for p in points], separators=(",", ":")),
        "record_file": key,
        "run_face_verify_id": face_verify_id,
        "run_check_res": json.dumps(
            build_run_check_res(identity, distance, used_time), separators=(",", ":")),
        "error_code": 3009, "is_timeout_zfb": 0,
    }, host, timeout)