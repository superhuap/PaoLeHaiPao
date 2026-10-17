#!/usr/bin/env python3
"""自检。不打真实服务端，跑一次几秒。

    python3 selftest.py

这些用例守的都是**已经踩过的坑**，删掉任何一条都会让相应的问题重新变成
「跑满一轮 7 分钟才炸出来」。
"""
from __future__ import annotations

import json
import os
import re
import sys
import shutil
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lptiyu as L      # noqa: E402
import runner as R       # noqa: E402
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "lptiyu.py")


def _write_account(**overrides) -> str:
    data = {"name": "t", "token": "T1", "uid": "20330647",
            "student_num": "26540E0115", "face_verify_id": "6acf1",
            "refresh_token": "R1", "face_file": "Public/x.jpg"}
    data.update(overrides)
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
    json.dump(data, handle)
    handle.close()
    return handle.name


class TestIdentityContract(unittest.TestCase):
    """identity() 提供的字段必须覆盖全部 identity[...] 读取。

    漏一个就是 KeyError，而它只会在跑到那一步时才暴露 —— 实测因此白等了
    7 分钟（renew_face_id 读 school_id、submit_run 读 face_verify_id）。
    """

    def test_identity_covers_every_read(self):
        with open(SRC, encoding="utf-8") as handle:
            source = handle.read()
        read = set(re.findall(r'identity\["([a-z_]+)"\]', source))
        provided = set(L.load_account(_write_account()).identity())
        self.assertEqual(read - provided, set(),
                         f"identity() 缺少 {sorted(read - provided)}，"
                         f"读到时会 KeyError")

    def test_rule_only_fields_are_not_on_identity(self):
        """school_id / term_id / game_id 来自规则对象，不该混进 identity。"""
        identity = L.load_account(_write_account()).identity()
        for key in ("school_id", "term_id", "game_id"):
            self.assertNotIn(key, identity,
                             f"{key} 应由 beforeRunV260 下发，从 rules 取")


class TestSignature(unittest.TestCase):
    """键值直连、盐只在末尾。写错则全流程 非法请求。"""

    def test_exact_form(self):
        import hashlib
        want = hashlib.md5(("a1" + "b2" + L.SIGN_SALT).encode()).hexdigest()
        self.assertEqual(L.sign({"b": 2, "a": 1}), want)

    def test_envelope_roundtrip(self):
        env = L.envelope({"ostype": 5, "nonce": "1"})
        body = L.decrypt(env["data"], L.API_KEY, L.API_IV)
        self.assertEqual(body["sign"],
                         L.sign({k: v for k, v in body.items() if k != "sign"}))


class TestReplyShapes(unittest.TestCase):
    def test_encrypted_data(self):
        blob = L.encrypt(json.dumps({"run_face_verify_id": "x"}), L.API_KEY, L.API_IV)
        self.assertEqual(L.interpret({"status": 1, "data": blob}),
                         {"run_face_verify_id": "x"})

    def test_plaintext_data(self):
        """Run2/faceVerify 直接回明文对象，不是加密串。"""
        self.assertEqual(
            L.interpret({"status": 1, "info": "人脸验证成功！",
                         "data": {"run_face_verify_id": "y"}}),
            {"run_face_verify_id": "y"})

    def test_missing_data_is_transport_error(self):
        with self.assertRaises(L.TransportError):
            L.interpret({"status": 1, "info": "人脸验证成功！"})

    def test_status_101_is_auth_error(self):
        with self.assertRaises(L.AuthError):
            L.interpret({"status": 101, "info": "登录信息失效，请重新登录"})

    def test_quota_is_its_own_error(self):
        """配额检查在时间检查之前，混淆会让「今天跑不了」看起来像配置坏了。"""
        with self.assertRaises(L.QuotaError):
            L.interpret({"status": 0, "info": "当天关联成绩次数已达到上限"})

    def test_duplicate_is_its_own_error(self):
        with self.assertRaises(L.DuplicateError):
            L.interpret({"status": 0, "info": "跑步记录已存在"})


class TestTimeline(unittest.TestCase):
    """时序错配的错误提示都是「跑步时间异常」，极具误导性，因此必须预检。"""

    def setUp(self):
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as handle:
            self.base = json.load(handle)

    def _config(self, **mut):
        import copy
        raw = copy.deepcopy(self.base)
        raw.update(mut)
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8")
        json.dump(raw, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return R.load_config(handle.name)

    def test_shipped_config_is_valid(self):
        R.load_config().validate()

    def test_end_pad_must_be_below_submit_tail(self):
        with self.assertRaises(L.ProtocolError) as ctx:
            self._config(end_pad=200).validate()
        self.assertIn("submit_tail", str(ctx.exception))

    def test_zero_interval_is_rejected(self):
        with self.assertRaises(L.ProtocolError) as ctx:
            self._config(report_interval=0).validate()
        self.assertIn("跑步时间异常", str(ctx.exception))

    def test_span_longer_than_window_is_rejected(self):
        with self.assertRaises(L.ProtocolError):
            self._config(report_interval=600).validate()

    def test_reports_start_near_window_head(self):
        """上报必须从窗口开头铺开。整体回拨窗口会把上报挤到末端，必被拒。"""
        cfg = R.load_config()
        tl = R.plan(cfg, at=1_000_000)
        offset = (tl.t0 - tl.start_time) / (tl.end_time - tl.start_time)
        self.assertLess(offset, 0.25,
                        f"首次上报落在窗口 {offset:.0%} 处，应贴近开头")

    def test_end_precedes_submit(self):
        cfg = R.load_config()
        tl = R.plan(cfg, at=1_000_000)
        self.assertLess(tl.end_time, tl.submit_at)

    def test_reports_inside_window(self):
        cfg = R.load_config()
        tl = R.plan(cfg, at=1_000_000)
        self.assertGreaterEqual(tl.t0, tl.start_time)
        self.assertLessEqual(tl.reports_end, tl.end_time)


class TestTrack(unittest.TestCase):
    def setUp(self):
        payload = {
            "school_id": 918, "term_id": 13280, "game_id": 2575,
            "time_rule_arr": [{"time_type": "3", "start_time": "21600",
                               "end_time": "73800", "min_log_num": "4",
                               "min_distance": "1.60", "max_distance": "10.00",
                               "min_pace": "540", "max_pace": "180"}],
            "run_line_info": {"point_max_distance2": 2.0, "point_list": [
                {"id": "29147", "jingwei": "37.06507296602183,122.440093483357"},
                {"id": "29148", "jingwei": "37.066545829708396,122.43996884819443"},
                {"id": "29149", "jingwei": "37.06604439742708,122.43956282980972"},
                {"id": "29150", "jingwei": "37.06603452705863,122.44062265353767"},
            ]},
        }
        self.rules = L.parse_rules(payload)

    def test_pace_bounds_unwind_inverted_naming(self):
        """min_pace=540 是慢的一端，max_pace=180 是快的一端。"""
        self.assertEqual((self.rules.pace_lo, self.rules.pace_hi), (180, 540))

    def test_segment_value_is_uniform(self):
        """各点同值。填累计里程会被按段测速算出 20 km/h 而被拒。"""
        points, _ = L.build_track(self.rules, 1.62, 375, 45, 1_000_000, 0.83)
        self.assertEqual(len({p.distance for p in points}), 1)

    def test_last_point_precedes_end_time(self):
        points, start = L.build_track(self.rules, 1.62, 375, 45, 1_000_000, 0.83)
        self.assertLess(points[-1].time, start + 375 + 45)
        self.assertEqual(start, 1_000_000 - 375 - 45)

    def test_checkpoints_come_from_response(self):
        self.assertEqual([c.id for c in self.rules.checkpoints],
                         [29147, 29148, 29149, 29150])

    def test_missing_rule_is_loud(self):
        with self.assertRaises(L.TransportError):
            L.parse_rules({"school_id": 1, "term_id": 1, "game_id": 1,
                           "time_rule_arr": [], "run_line_info": {}})

    def test_run_check_res_shape(self):
        identity = L.load_account(_write_account()).identity()
        res = L.build_run_check_res(identity, 1.62, 375)
        self.assertEqual([r["plan"] for r in res], [1, 4, 5, 6])
        self.assertEqual(len(res[0]["inteval_result"].split(",")), 375 // 30)
        self.assertRegex(res[0]["inteval_value"].split(",")[0], r"^0-\d+\.\d+$")
        self.assertRegex(res[2]["inteval_value"].split(",")[0], r"^\d+-0$")
        for entry in res:
            self.assertEqual((entry["is_cheat"], entry["is_stop"]), (0, 0))


class TestCredentialSafety(unittest.TestCase):
    """续期会作废旧的一对，返回值丢不得。"""

    def test_save_field_is_atomic_and_keeps_staged_on_failure(self):
        path = _write_account()
        account = L.load_account(path)
        self.addCleanup(os.unlink, path)
        L.save_field(account, "token", "T2")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["token"], "T2")

        staged = f"{path}.tmp"
        with unittest.mock.patch.object(L.os, "replace",
                                       side_effect=OSError("Read-only file system")):
            with self.assertRaises(L.ProtocolError) as ctx:
                L.save_field(account, "token", "T3")
        try:
            self.assertTrue(os.path.exists(staged),
                            "暂存文件被删就等于把新值销毁了")
            self.assertIn(staged, str(ctx.exception))
        finally:
            if os.path.exists(staged):
                os.unlink(staged)

    def test_save_field_skips_when_unchanged(self):
        path = _write_account()
        self.addCleanup(os.unlink, path)
        account = L.load_account(path)
        L.save_field(account, "token", account.token)
        self.assertFalse(os.path.exists(f"{path}.tmp"))


class TestAtomicCredentialWrite(unittest.TestCase):
    """token 与 refresh_token 必须一次写完。

    续期让旧的一对同时作废。分两次写会在中间留下「新 token + 已作废的
    refresh」—— 文件看着正常却再也无法自愈，报出来的是「凭据对已耗尽」，
    极难归因。实测两个账号都因此反复踩过。
    """

    def setUp(self):
        self.path = _write_account()
        self.addCleanup(os.unlink, self.path)

    def test_pair_written_in_one_replace(self):
        account = L.load_account(self.path)
        calls = []
        real = L.os.replace

        def counting(src, dst):
            calls.append(dst)
            return real(src, dst)

        with unittest.mock.patch.object(L.os, "replace", counting):
            L.save_fields(account, {"token": "T2", "refresh_token": "R2"})
        self.assertEqual(len(calls), 1, f"应只替换一次，实际 {len(calls)} 次")
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual((saved["token"], saved["refresh_token"]), ("T2", "R2"))

    def test_no_torn_state_when_replace_fails(self):
        """写失败时磁盘必须保持原样，不能留下半对。"""
        account = L.load_account(self.path)
        with unittest.mock.patch.object(L.os, "replace",
                                       side_effect=OSError("Read-only file system")):
            with self.assertRaises(L.ProtocolError):
                L.save_fields(account, {"token": "T2", "refresh_token": "R2"})
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual((saved["token"], saved["refresh_token"]),
                         ("T1", "R1"), "失败后原文件被改动了")

    def test_staged_copy_holds_the_new_pair(self):
        """暂存文件是唯一的恢复路径，必须留着且内容完整。"""
        account = L.load_account(self.path)
        staged = f"{self.path}.tmp"
        self.addCleanup(lambda: os.path.exists(staged) and os.unlink(staged))
        with unittest.mock.patch.object(L.os, "replace",
                                       side_effect=OSError("Read-only file system")):
            with self.assertRaises(L.ProtocolError):
                L.save_fields(account, {"token": "T2", "refresh_token": "R2"})
        self.assertTrue(os.path.exists(staged))
        with open(staged, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual((saved["token"], saved["refresh_token"]), ("T2", "R2"))

    def test_unchanged_values_skip_write(self):
        account = L.load_account(self.path)
        self.assertFalse(L.save_fields(account, {"token": "T1", "refresh_token": "R1"}))
        self.assertFalse(os.path.exists(f"{self.path}.tmp"))

    def test_blank_values_never_overwrite(self):
        """值为 None/空 的键必须被跳过，不能把已有值抹成空。"""
        account = L.load_account(self.path)
        L.save_fields(account, {"token": "T2", "refresh_token": None})
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["token"], "T2", "非空字段应当写入")
        self.assertEqual(saved["refresh_token"], "R1", "空值不应覆盖已有凭据")


class TestQuotaClassification(unittest.TestCase):
    """配额用满是「今天跑不了」，不是故障。

    服务端以 status=1 正常返回，把原因放进 data.record_failed_reason —— 只看
    信封层的 info 一无所获，配额会掉进「record_status != 1」的通用分支被判成
    失败，批处理因此以退出码 1 结束，CI 变红。
    """

    def test_quota_reason_in_payload_raises_quota(self):
        with self.assertRaises(L.QuotaError):
            L.classify_rejection({"record_status": 0,
                                  "record_failed_reason": "当天关联成绩次数已达到上限"})

    def test_duplicate_reason_in_payload_raises_duplicate(self):
        with self.assertRaises(L.DuplicateError):
            L.classify_rejection({"record_status": 0,
                                  "record_failed_reason": "跑步记录已存在，请勿重复提交"})

    def test_valid_record_passes(self):
        L.classify_rejection({"record_status": 1, "record_id": 1})

    def test_unknown_reason_is_left_to_caller(self):
        """认不出的原因不能吞掉 —— 得让调用方按无效处理。"""
        L.classify_rejection({"record_status": 0,
                              "record_failed_reason": "跑步时间异常"})
        L.classify_rejection({"record_status": 0})


def _before_payload(**extra):
    """造一份够 parse_rules 用的 beforeRunV260 载荷。"""
    payload = {
        "school_id": 918, "term_id": "77", "game_id": "88",
        "time_rule_arr": [{"time_type": "3", "min_distance": 1.0,
                           "max_distance": 5.0, "min_pace": 540,
                           "max_pace": 180, "min_log_num": 4,
                           "start_time": 18000, "end_time": 82800}],
        "run_line_info": {"point_max_distance2": 2.0,
                          "point_list": [{"id": 1, "jingwei": "37.1,122.1"}]},
    }
    payload.update(extra)
    return payload


_IDENTITY = {"uid": "20330647", "student_num": "26540E0115",
             "term_id": "", "game_id": "", "school_id": "", "token": "T"}

FACE_OK = "人脸验证成功！"
FACE_BLUR = "未拍到完整人脸"
# 实测拿到的真实失败文案（record_id=31992 那次 cyh 的判定）
FACE_FAIL = "人脸验证未通过，请重新验证!"


def _face_env(face_id="abc123def456", info=FACE_OK):
    """faceVerify 的信封：注意失败时**照样**带 run_face_verify_id。"""
    return {"status": 1, "info": info,
            "data": {"run_face_verify_id": face_id}}


class _FakeFaceAPI:
    """按脚本回放 faceVerify 应答，并记录每次请求。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, path, params, host, timeout):
        self.calls.append(dict(params))
        return self.script.pop(0) if self.script else _face_env()


class TestFaceVerification(unittest.TestCase):
    """必须读服务端写在信封 info 里的判定结论。

    只看 run_face_verify_id 非空是不够的：type=0 失败时**照样返回 id**
    （PROTOCOL.md §3.5）。此前代码因此把失败当成功，一路带到 stopRun 才
    炸成「人脸验证不合格」，实测白等 425s。
    """

    def setUp(self):
        self.rules = L.parse_rules(_before_payload())

    def _run(self, fake, face_file="Public/Upload/pic/face_verify/x.jpg"):
        with unittest.mock.patch.object(L, "call_envelope", fake):
            return L.renew_face_id(_IDENTITY, self.rules, face_file, "h", 5)

    def test_failed_type0_is_retried_with_the_returned_id(self):
        """失败后带着原 id 再调一次 —— 这正是真机的行为。"""
        fake = _FakeFaceAPI([_face_env(info=FACE_BLUR),
                             _face_env(info=FACE_OK),      # 重试 type=0
                             _face_env(), _face_env()])    # type=2 x2
        self._run(fake)
        self.assertEqual(len(fake.calls), 4)
        first, retry = fake.calls[0], fake.calls[1]
        self.assertEqual(first["type"], 0)
        self.assertEqual(retry["type"], 0)
        self.assertEqual(retry.get("run_face_verify_id"), "abc123def456",
                         "重试必须带上失败时返回的那个 id")

    def test_retry_exhausted_raises_with_server_wording(self):
        """重试仍失败要抛异常，且带上服务端原话便于定位。"""
        fake = _FakeFaceAPI([_face_env(info=FACE_BLUR) for _ in range(4)])
        with self.assertRaises(L.ProtocolError) as ctx:
            self._run(fake)
        self.assertIn(FACE_BLUR, str(ctx.exception))

    def test_success_wording_does_not_retry(self):
        fake = _FakeFaceAPI([_face_env(), _face_env(), _face_env()])
        self._run(fake)
        self.assertEqual(len(fake.calls), 3, "成功文案不该触发重试")
        self.assertEqual([c["type"] for c in fake.calls], [0, 2, 2])

    def test_type2_response_is_not_discarded(self):
        """type=2 的应答同样要检查，不能拿到就扔。"""
        fake = _FakeFaceAPI([_face_env(),
                             _face_env(info=FACE_BLUR),    # 第一次确认失败
                             _face_env()])
        with self.assertRaises(L.ProtocolError) as ctx:
            self._run(fake)
        self.assertIn(FACE_BLUR, str(ctx.exception))

    def test_all_steps_report_server_wording(self):
        """三步各自的判定都要进日志，否则排查仍然靠猜。"""
        fake = _FakeFaceAPI([_face_env(), _face_env(), _face_env()])
        with self.assertLogs("runner", level="INFO") as cap:
            self._run(fake)
        self.assertIn(FACE_OK, " ".join(cap.output))

    def test_verdict_requires_positive_confirmation(self):
        """只有明说「成功」才放行。

        这里曾用失败关键词黑名单，结果服务端真实的「未通过」不在表里 ——
        明说没过却被放行，一路带到 stopRun 炸成「人脸验证不合格」，白等 425s。
        黑名单天生漏，必须改成「要求成功」。
        """
        self.assertFalse(L._face_failed(FACE_OK))
        self.assertTrue(L._face_failed(FACE_FAIL), "真实失败文案必须判为失败")
        self.assertTrue(L._face_failed(FACE_BLUR))
        self.assertTrue(L._face_failed(""))
        self.assertTrue(L._face_failed("人脸比对未通过"))

    def test_unknown_wording_is_treated_as_failure(self):
        """没见过的文案不能默认放行 —— 那正是上次漏判的成因。"""
        for msg in ("请稍后重试", "系统繁忙", "0", "OK"):
            with self.subTest(msg=msg):
                self.assertTrue(L._face_failed(msg))

    def test_real_failure_wording_triggers_retry_then_raises(self):
        """用实测文案跑一遍完整流程：必须重试、最终抛出且带上原文。"""
        fake = _FakeFaceAPI([_face_env(info=FACE_FAIL) for _ in range(4)])
        with self.assertRaises(L.ProtocolError) as ctx:
            self._run(fake)
        self.assertIn(FACE_FAIL, str(ctx.exception))
        self.assertEqual(len(fake.calls), 2,
                         "type=0 重试一次仍失败就该中止，type=2 不必再发")


class TestServerCounters(unittest.TestCase):
    """服务端记账：total_num 有效次数 / run_num 今日 / term_num 学期目标。

    这三个数在实测中序列化类型并不一致（total_num 是数字 8，run_num 是
    字符串 '2'），所以容错转换必须两头都吃；取不到时也不能炸 ——
    载荷有 100+ 字段，缺一个计数不该让整个流程失败。
    """

    def test_extracts_counters(self):
        rules = L.parse_rules(_before_payload(total_num=8, run_num="2",
                                              term_num="50"))
        self.assertEqual(rules.valid_runs, 8)
        self.assertEqual(rules.today_runs, 2)
        self.assertEqual(rules.term_target, 50)

    def test_accepts_both_number_and_string(self):
        rules = L.parse_rules(_before_payload(total_num="9", run_num=3,
                                              term_num=50))
        self.assertEqual((rules.valid_runs, rules.today_runs, rules.term_target),
                         (9, 3, 50))

    def test_missing_counters_default_to_zero(self):
        rules = L.parse_rules(_before_payload())
        self.assertEqual((rules.valid_runs, rules.today_runs, rules.term_target),
                         (0, 0, 0))

    def test_garbage_counters_do_not_raise(self):
        for junk in ("", "  ", "-", None, "abc", [], {}):
            with self.subTest(junk=junk):
                rules = L.parse_rules(_before_payload(total_num=junk))
                self.assertEqual(rules.valid_runs, 0)

    def test_as_int_rejects_bool(self):
        """True 不该被当成 1 —— 布尔在这里只可能是标记位。"""
        self.assertEqual(L._as_int(True), 0)


class TestProgressReporting(unittest.TestCase):
    """进度展示不能反过来把账号判成失败。"""

    def _cfg(self):
        # 只用到 host / timeout，不值得为测试拼一整个 Config
        return SimpleNamespace(host="h", timeout=1)

    def test_dry_run_makes_no_request(self):
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise AssertionError("演练不该发请求")

        before = L.parse_rules(_before_payload(total_num=8, term_num=50))
        with unittest.mock.patch.object(L, "fetch_rules", boom):
            R._report_progress(SimpleNamespace(name="a", identity=dict),
                               self._cfg(), before, dry_run=True)
        self.assertEqual(calls, [])

    def test_refetch_failure_falls_back_without_raising(self):
        before = L.parse_rules(_before_payload(total_num=8, run_num="2",
                                               term_num="50"))
        with unittest.mock.patch.object(L, "fetch_rules",
                                       side_effect=L.ProtocolError("网络抽风")):
            with self.assertLogs("runner", level="INFO") as cap:
                R._report_progress(SimpleNamespace(name="a", identity=dict),
                                   self._cfg(), before, dry_run=False)
        text = " ".join(cap.output)
        self.assertIn("有效 8/50", text)
        self.assertIn("未能刷新", text, "该标明是兜底值，不能让人误读成最新数")

    def test_prefers_freshly_fetched_value(self):
        before = L.parse_rules(_before_payload(total_num=8, term_num=50))
        after = L.parse_rules(_before_payload(total_num=9, run_num="3",
                                              term_num=50))
        with unittest.mock.patch.object(L, "fetch_rules", return_value=after):
            with self.assertLogs("runner", level="INFO") as cap:
                R._report_progress(SimpleNamespace(name="a", identity=dict),
                                   self._cfg(), before, dry_run=False)
        text = "\n".join(cap.output)
        self.assertIn("有效 9/50", text)
        self.assertIn("今日 3", text)
        self.assertNotIn("未能刷新", text)


class TestFaceRepublishLayout(unittest.TestCase):
    """人脸照片重传：同一张图换新路径就能恢复，不必重新拍照。"""

    def test_key_matches_real_device_layout(self):
        import datetime
        key = L.face_object_key(datetime.datetime(2026, 10, 17, 12, 0))
        head = "Public/Upload/pic/face_verify/2026-10-17/"
        self.assertTrue(key.startswith(head), key)
        tail = key[len(head):]
        folder, _, name = tail.partition("/")
        self.assertEqual(len(folder), 3, "三位数目录")
        self.assertTrue(folder.isdigit())
        self.assertTrue(name.endswith(".jpg"))
        self.assertEqual(len(name), 36 + 4, "uuid + .jpg")

    def test_keys_are_unique(self):
        keys = {L.face_object_key() for _ in range(50)}
        self.assertEqual(len(keys), 50, "每次都应生成不同 key，避免互相覆盖")

    def test_upload_declares_image_content_type(self):
        """上传器已泛化；人脸照片必须声明 image/jpeg。"""
        source = open(os.path.join(os.path.dirname(L.__file__), "lptiyu.py"),
                      encoding="utf-8").read()
        self.assertIn("content_type=\"image/jpeg\"", source)
        self.assertIn("filename=\"face.jpg\"", source)


class TestAutoRepublish(unittest.TestCase):
    """人脸核验失败后自动重传照片再试。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_missing_photo_returns_none(self):
        """照片是可选增强，没配不能崩 —— 调用方据此退回原行为。"""
        self.assertIsNone(L.load_face_image(self.dir, "cyh"))

    def test_empty_file_is_not_returned(self):
        open(os.path.join(self.dir, "cyh.jpg"), "wb").close()
        self.assertIsNone(L.load_face_image(self.dir, "cyh"),
                          "空文件不能当照片传上去")

    def test_reads_each_extension(self):
        for suffix in (".jpg", ".jpeg", ".png"):
            with self.subTest(suffix=suffix):
                path = os.path.join(self.dir, f"p{suffix}")
                open(path, "wb").write(b"x")
                self.assertEqual(L.load_face_image(self.dir, "p"), b"x")

    def test_picture_dir_resolves_relative_to_project(self):
        """相对路径必须按工程目录解析，不能依赖进程 cwd。"""
        self.assertEqual(R._picture_dir("picture"),
                         os.path.join(R.HERE, "picture"))
        self.assertEqual(R._picture_dir("/data/picture"), "/data/picture")
        self.assertEqual(R._picture_dir(None), R.PICTURE_DIR)

    def test_default_is_enabled(self):
        """默认开着 —— 实测重传一次就能救回来，代价只有一发 OSS 上传。"""
        self.assertTrue(R.load_config().auto_republish_face)

    def test_republish_never_touches_token(self):
        """重传只改人脸两项，凭据必须原样。

        重传的落盘要和 token 续期一样走 save_fields —— 后者一旦被改成
        只写人脸字段，token 对被撕裂的坑就会重新出现。
        """
        path = _write_account(face_file="Public/old.jpg", face_verify_id="old-id")
        self.addCleanup(os.unlink, path)
        account = L.load_account(path)
        self.assertTrue(L.save_fields(account, {
            "face_file": "Public/new.jpg", "face_verify_id": "new-id"}))
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["face_file"], "Public/new.jpg")
        self.assertEqual(saved["face_verify_id"], "new-id")
        self.assertEqual(saved["token"], "T1")
        self.assertEqual(saved["refresh_token"], "R1")

    def test_face_file_and_id_survive_together(self):
        """两个字段一次写入 —— 中途断掉不能留下半对。"""
        path = _write_account(face_file="Public/old.jpg", face_verify_id="old-id")
        self.addCleanup(os.unlink, path)
        account = L.load_account(path)
        L.save_fields(account, {"face_file": "Public/new.jpg"})
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["face_file"], "Public/new.jpg")
        self.assertEqual(saved["face_verify_id"], "old-id",
                         "未提供的字段不得被清空")

    def test_face_verify_error_is_protocol_error_subclass(self):
        """refresh_face 的 except 分支要能兜住。"""
        self.assertTrue(issubclass(L.FaceVerifyError, L.ProtocolError))


class TestLogAttribution(unittest.TestCase):
    """多账号并行时协议层日志必须各归各位。

    账号维度原先是模块级 dict，三个线程共享，谁最后绑定谁的号就盖掉全部 ——
    控制台上三个账号的报文全标成同一个号，排查时会指错方向。
    """

    def test_bind_is_per_thread(self):
        import threading as th
        seen = {}
        barrier = th.Barrier(2)

        def worker(name):
            scope = L.bind_account(name)
            barrier.wait()                    # 强制两个线程同时处于绑定态
            seen[name] = L._EXTRA()["account"]
            L.reset_account(scope)

        threads = [th.Thread(target=worker, args=(n,))
                   for n in ("alpha", "beta")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(seen, {"alpha": "alpha", "beta": "beta"})

    def test_reset_restores_previous_binding(self):
        outer = L.bind_account("outer")
        self.addCleanup(lambda: L.reset_account(outer))
        inner = L.bind_account("inner")
        self.assertEqual(L._EXTRA()["account"], "inner")
        L.reset_account(inner)
        self.assertEqual(L._EXTRA()["account"], "outer")

    def test_unbound_defaults_to_scheduler(self):
        scope = L.bind_account(None)
        self.addCleanup(lambda: L.reset_account(scope))
        self.assertEqual(L._EXTRA()["account"], "调度")


class TestConfigValidation(unittest.TestCase):
    def test_end_pad_ge_tail_blocked(self):
        cfg = R.Config(
            host="h", oss_endpoint="o", timeout=20, distance=1.62, used_time=375,
            end_margin=45, segment_ratio=0.83, report_count=6, report_interval=60,
            end_pad=120, submit_tail=120, renew_face_before_submit=True,
            start_stagger_seconds=10)
        with self.assertRaises(L.ProtocolError):
            cfg.validate()

    def test_account_unknown_field_rejected(self):
        path = _write_account(tokne="x")
        self.addCleanup(os.unlink, path)
        with self.assertRaises(L.ProtocolError):
            L.load_account(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)