# runner

多账号跑步记录提交调度器。一个账号一个 JSON，参数集中在 `config.json`，
账号间隔若干秒依次启动因而并行。

```
runner/
├── config.json        跑步参数（距离、用时、上报节奏、并行间隔）
├── lptiyu.py          协议层
├── logs.py            日志（仅控制台）
├── selftest.py        自检（不打网络，秒级）
├── runner.py          调度器
├── accounts/
│   ├── cyh.json       每个文件 = 一个账号
│   ├── lzy.json
│   └── pjh.json
└── README.md
```

## 用法

```bash
pip install pycryptodome

python3 selftest.py                # 改完代码先跑这个，秒级，不打网络
python3 runner.py --check          # 只体检账号，不发请求
python3 runner.py --dry-run        # 按真实时序演练，不发请求
python3 runner.py                  # 跑全部启用账号
python3 runner.py lzy              # 只跑指定账号
python3 runner.py --serial         # 串行，不错峰
```

退出码：`0` 全部成功或跳过，`1` 有失败，`2` 配置问题。

**改完代码先跑 `selftest.py`。** 27 条用例守的都是已经踩过的坑，其中最要紧的
一条：扫描 `lptiyu.py` 里所有 `identity[...]` 读取，校验 `Account.identity()`
都提供了对应字段。漏字段不会在加载时暴露，而是跑到那一步才 `KeyError` ——
实测因此白等过 7 分钟（`renew_face_id` 读 `school_id`、
`submit_run` 读 `face_verify_id`，各中过一次）。

### 日志

**只输出到控制台（stderr），不写任何文件。** 需要留档由 shell 负责：

```bash
python3 runner.py > /dev/null 2>> run.log        # 追加到文件
python3 runner.py 2>&1 | tee run.log            # 同时看屏和留档
python3 runner.py 2>/dev/null                    # 完全静音
```

格式为 `时间  级别  [账号] 内容`：

```
2026-10-16 16:45:14  INFO   [cyh] 1.62 km / 375s / 配速 231s/km (规则 180–540)
2026-10-16 16:45:14  WARNING [lzy] 体检 token 已失效
```

```bash
python3 runner.py --log-level DEBUG     # 记录收发报文（已脱敏）
python3 runner.py --quiet               # 只显示警告以上
```

级别约定：`DEBUG` 逐步细节（每次上报、收发报文）、`INFO` 流程节点、`WARNING`
降级但继续（配额满、距离被收敛、token 中途续期）、`ERROR` 失败。

**凭据一律脱敏。** DEBUG 级记录的是**解密后的明文**（密文既不可读，按字段名匹配的
脱敏规则也作用不上），其中 `token` / `access_token` / `refresh_token` /
`run_face_verify_id` / `sign` 会被替换成首尾片段：

```
→ beforeRunV260 {"uid": "20330647", "token":"2BDE…AD4A", "access_token":"2BDE…AD4A", …}
```

终端回滚缓冲、tmux、录屏都可能把明文带出去，脱敏因此仍然必要。

## 配置

### `config.json` —— 跑步参数

```json
{
  "distance": 1.62,
  "used_time": 375,
  "end_margin": 45,
  "segment_ratio": 0.83,
  "report_count": 6,
  "report_interval": 60,
  "end_pad": 100,
  "submit_tail": 120,
  "start_stagger_seconds": 10
}
```

`school_id` / `term_id` / `game_id`、判分阈值、打卡点坐标、有效时段**不在这里**，
每次运行从 `beforeRunV260` 现取。换学期不必改文件。

### `accounts/<名字>.json` —— 一个账号

```json
{
  "name": "lzy",
  "enabled": true,
  "token": "…",
  "refresh_token": "…",
  "uid": "20330559",
  "student_num": "26540E0113",
  "face_verify_id": "…",
  "face_file": "Public/Upload/pic/face_verify/…"
}
```

只有 `token` / `uid` / `student_num` 必填。其余决定了这个账号能不能自愈：

| 字段 | 缺了会怎样 |
|---|---|
| `refresh_token` | token 失效即失败，需重新抓包。**强烈建议配置** |
| `face_file` | `face_verify_id` 失效即失败，需重新抓包 |

未知字段会被拒绝（防止 `tokne` 这类拼写错误静默失效）。`chmod 600`。

### 自动写回

`token`、`refresh_token`、`face_verify_id` 都会被服务端作废，脚本在续期/重核成功后
立刻原子写回对应文件。用临时文件 + `os.replace`，若替换失败会保留暂存文件并在
错误里报出位置 —— 那是唯一的恢复路径。

## 为什么每个账号要等 7 分钟

这不是保守设计，是服务端三条硬约束叠出来的：

1. **跑步窗口下限 288 秒**。`used_time = 距离 × 配速`，配速合法区间 180–540
   秒/公里，最短 1.60 km 配 180 秒/公里就是 288 秒。窗口短不了。
2. **实时上报必须真实等待**。`setRunLocationRecord` 由服务端按**请求到达的
   真实时刻**记录，无法回拨。
3. **上报要从窗口开头铺开**。窗口整体往回拨虽然能免掉等待，但会把上报全挤到
   窗口末端 —— 实测判「跑步时间异常」。

外加 `end_time` 必须严格早于提交时刻（零延迟完成会被拒）。

于是：窗口 420 秒，上报 6×60 秒铺满前 300 秒，末次上报后再等 120 秒提交。

### 改参数前必读

`end_pad` **必须小于** `submit_tail`，否则 `end_time` 会落到未来，触发零延迟
拒绝 —— 而这个错误的提示是「跑步时间异常」，极具误导性。

`report_interval` 调小到 0（连发）或 30（覆盖窗口 36%）都会被判「跑步时间异常」
（实测过）。`Config.validate()` 会在发请求前挡住这两种配错。

## 提交前服务端强制要求的两件事

**1. 跑步窗口内铺满实时上报。** 缺失判「跑步时间异常」。按 `beforeRunV260`
下发的 `run_check` plan 1 执行（每 30 秒一次，共 5 次），脚本取 6 次 × 60 秒。

**2. 走完人脸核验三步。** `faceVerify` 的 `type=0` 取 id，再 `type=2` 确认两次
（`middle_num=2`）。**只做 `type=0` 拿到的 id 提交会被判「人脸验证不合格」。**

这两个接口在小程序信封下都能直接调用，**不需要 `lp31`、不需要跑步层私有盐**，
只要给一个此前成功核验时上传过的 `face_file` 路径。

## 失败要尽早暴露

流程里有 7 分钟的真实等待，出错越晚代价越大。因此**人脸重核放在实时上报之前**
先做一遍：凭据、人脸照片这类问题的发生概率远高于轨迹本身，若放在等待之后才验，
一次出错就是 7 分钟。提交前会再重核一次以保证 id 新鲜。

worker 结束时会立刻记一条带耗时的结果，不必等全部账号跑完才知道谁挂了。

## 每日配额

实测上限 **2 条/天**。配额检查在时间检查之前，所以配额用完的账号会一直返回
`当日关联成绩次数已达到上限`，**看不出它的其他配置是否正常**。用 `--check`
体检更可靠。

## 依赖与排查

- `pycryptodome` 是唯一外部依赖
- 所有网络错误归类为 `TransportError`，鉴权归 `AuthError`，业务拒绝按原因分类
- 单账号失败不影响其它账号；末尾汇总成功/失败/跳过