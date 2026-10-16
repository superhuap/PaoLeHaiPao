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
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lptiyu as L      # noqa: E402
import runner           # noqa: E402

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
        return runner.load_config(handle.name)

    def test_shipped_config_is_valid(self):
        runner.load_config().validate()

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
        cfg = runner.load_config()
        tl = runner.plan(cfg, at=1_000_000)
        offset = (tl.t0 - tl.start_time) / (tl.end_time - tl.start_time)
        self.assertLess(offset, 0.25,
                        f"首次上报落在窗口 {offset:.0%} 处，应贴近开头")

    def test_end_precedes_submit(self):
        cfg = runner.load_config()
        tl = runner.plan(cfg, at=1_000_000)
        self.assertLess(tl.end_time, tl.submit_at)

    def test_reports_inside_window(self):
        cfg = runner.load_config()
        tl = runner.plan(cfg, at=1_000_000)
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


class TestConfigValidation(unittest.TestCase):
    def test_end_pad_ge_tail_blocked(self):
        cfg = runner.Config(
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