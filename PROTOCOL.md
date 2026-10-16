# 步道乐跑跑步记录提交 —— 协议与实现参考

面向改代码的人。安全评估结论见仓库根目录的 `docs.md`，本文只讲「怎么实现、
哪里有坑」。

本文中的实例数据一律用占位符：`<access_token>`、`<uid>`、`<student_num>`、
`<face_verify_id>`、`<face_file>`、`<record_id>`。

---

## 1 · 概述与文件地图

一次提交的完整调用链：

```
fetch_rules()          beforeRunV260      取身份三元组 + 判分阈值 + 打卡点坐标
   ↓
plan()                 由上报时刻表反推 start/end/used_time
   ↓
build_track()          铺 GPS 点，log_data[].distance 各点同值
   ↓
preflight()            比对服务端阈值，不满足就不发请求
check_window()         当天有效时段内
   ↓
renew_face_id()        faceVerify type=0 → type=2 ×2      ← 提交前必做
   ↓
report_location() ×6   setRunLocationRecord，间隔 60 秒     ← 提交前必做
   ↓
register_checkpoints() getStudentVenaPoint
fetch_sts()            getOssSts          取 OSS 临时凭证
upload_track()         OSS 表单上传
   ↓
submit_run()           stopRunV278
```

| 文件 | 职责 |
|---|---|
| `lptiyu.py` | 协议层。加解密、传输、规则解析、轨迹构造、三步流程、凭据 |
| `runner.py` | 调度层。配置加载、时序编排、多账号错峰并行、结果汇总 |
| `logs.py` | 日志。仅控制台，凭据脱敏 |
| `selftest.py` | 自检。27 条，秒级，不打网络 |
| `config.json` | 跑步参数。距离、用时、上报节奏、并行间隔 |
| `accounts/*.json` | 每个文件 = 一个账号 |

**`selftest.py` 与 `--check` 的分工**：前者验代码不变量（离线，秒级），
后者验账号配置是否具备自愈能力（要打一次网络）。

---

## 2 · 加解密

### 2.1 两层 AES

通信用两层独立的 AES-CBC-PKCS5。

| 层 | Key | IV | 承载 |
|---|---|---|---|
| **API 层** | `Wet2C8d34f62ndi3` | `K6iv85jBD8jgf32D` | 所有响应的 `data` 字段；小程序信封的内层明文 |
| **跑步层** | `goMEzJ1pDpncNHb9` | `ZUrAS2lUidjyY2gK` | 真机 `key=<base64>` 密文请求体 |

本脚本全程走 API 层，跑步层密钥用不上。

### 2.2 签名

```
sign = md5(''.join(k + v for k in sorted(params)) + 'rDJiNB9j7vD2')
```

公开盐 `rDJiNB9j7vD2`。三个必须记对的点：

1. **键值直接相连，不加等号** —— 写成 `f"{k}={v}"` 全流程返回 `非法请求`（踩过）
2. **盐只在结尾，不在首尾各一** —— 写成 `salt + payload + salt` 同样 `非法请求`（踩过）
3. **空值字段照样参与签名** —— 解析表单必须保留空值：

```python
urllib.parse.parse_qs(body, keep_blank_values=True)
```

形如 `error_code=` 的字段被丢掉会导致签名校验失败。这坑在 `docs.md` §2.3 也记着。

### 2.3 小程序信封

```python
body = dict(params)
body["sign"] = md5(...见上...)
form  = {"ostype": "5",
         "data": base64(AES_CBC_PAD(json.dumps(body, separators=(",", ":"))))}
```

鉴权中间件按**请求格式**而非语义授权，`ostype=5&data=` 这套格式不被识别，
因而能穿透到业务逻辑。真机的 `key=` 格式则一律被拦。

### 2.4 OSS 上传

```
policy = base64(json({"expiration": <now+1h>,
                      "conditions": [["content-length-range", 0, 1073741824]]}))
signature = base64(HMAC-SHA1(AccessKeySecret, policy))
```

三个坑：

- **必须 POST 到 bucket 根路径**，object key 放在表单字段里。
  PUT 到对象路径会被拒（`AccessDenied ... because of bucket acl`）——
  临时密钥只授了表单上传的权限。
- **成功码是 204**，不是 200。写成只接受 200 会把成功报成失败。
- 不要加 `success_action_status` 字段（真机没有，加了会触发 bucket ACL 拒绝）。

请求头里 `Origin: https://servicewechat.com` 与 `aliyun-sdk-js/6.18.0` 的
User-Agent 沿用真机值。

### 2.5 跑步层私有盐

**未获取**。仅用于跑步层内层签名与部分原生接口。本脚本用不到。

---

## 3 · 接口清单

全部走 `/v3/api.php/` 前缀，信封通道。

### 3.1 `Run2/beforeRunV260` — 取规则

**只需 `<token>` + `<uid>` + `<student_num>`**，不需要先有 `term_id`。
请求里写错的 `term_id` 会被服务端忽略并返回当前生效值 —— 这是账号 JSON
不必写 `term_id` 的原因，换学期时不用改文件。

返回 100+ 字段，重要的几组：

| 字段 | 内容 |
|---|---|
| `school_id` / `term_id` / `game_id` | 身份三元组 |
| `time_rule_arr[]` | `min_distance` `max_distance` `min_pace` `max_pace` `min_log_num` `start_time` `end_time` |
| `run_line_info.point_list[]` | 打卡点 `id` 与 `jingwei`（`"纬度,经度"`） |
| `run_line_info.point_max_distance2` | 相邻 GPS 点最大间距（围栏） |
| `run_check[]` | 跑步期检查计划，见 §5.2 |
| `run_face_verify_config` | `middle_status` `middle_distance` `middle_num`，见 §3.5 |

**配速字段命名反直觉**：`min_pace=540` 是慢的一端，`max_pace=180` 是快的一端。
解析时排序取 `[180, 540]`。

> ⚠️ **历史教训**：这些数值早先被硬编码在源码里，服务端改规则后脚本静默失效，
> 且无从判断是规则变了还是参数错了。现一律现取。

### 3.2 `WpIndex/getOssSts` — 取临时凭证

入参同上。返回 `AccessKeyId` / `AccessKeySecret` / `SecurityToken` / `bucket` /
`endpoint`。凭证不与 token 绑定，跨续期复用是安全的。

### 3.3 `Run/stopRunV278` — 提交

**四个字段必须是字符串**：`distance` / `used_time` / `start_time` / `end_time`。
传 float 得到 `非法请求` —— 而且是在进入业务逻辑**之前**就被判废，
不会给出可定位的类型错误。

其余要点见 §5。

### 3.4 `Login/RefreshToken` — 续期

见 §7。

### 3.5 `Run2/faceVerify` — 人脸核验

真机上是拍照 + 活体检测。分两步，**都要带 `face_file`**：

| 请求 | 含义 |
|---|---|
| `type=0` | 提交照片，取回 `run_face_verify_id` |
| `type=2` | 中途确认。`middle_num=2` 表示要两次 |

两个关键实测结论：

1. **`type=0` 失败时同样返回 `run_face_verify_id`。**
   真机第一次拍照失败（`未拍到完整人脸`），响应里仍带了 id，
   下一次 `type=0` 带着它就通过了。
2. **只做 `type=0` 拿到的 id 提交会被判「人脸验证不合格」。**
   必须走完 `type=2` 才是服务端认可的完整核验（证据：`record_id=30598`）。

> **`type=2` 不带 `face_file` 会报「参数异常」** —— `type=0` 可以不带，
> `type=2` 必须带。

这个接口在小程序信封下可直接调用，**不需要 `lp31`、不需要跑步层私有盐**。

### 3.6 `Run2/getStudentVenaPoint` — 登记打卡点

传 `point_info`（4 个打卡点 id 的 JSON 数组，顺序随机）。
响应无业务数据，只有 `{"status":1,"info":""}`。

> **不要用 `_interpret` 解析它** —— 那个函数要求 `data` 是加密串，
> 会把「无 data」误判成传输错误。用 `_accept`。

### 3.7 `Run/setRunLocationRecord` — 实时位置上报

传 `latitude` / `longitude`（当前位置）与 `point_ids`（**每次都带全部 4 个**，顺序随机）。

**服务端按请求到达的真实时刻记录，无法回拨。** 这是 §6 全部时间约束的根源。

同样**不需要 `lp31`**。

---

## 4 · 数据结构

### 4.1 账号文件 `accounts/<名字>.json`

```json
{
  "name": "<名字>",
  "enabled": true,
  "token": "<access_token>",
  "refresh_token": "<refresh_token>",
  "uid": "<uid>",
  "student_num": "<student_num>",
  "face_verify_id": "<run_face_verify_id>",
  "face_file": "<Public/Upload/pic/face_verify/...>"
}
```

只有 `token` / `uid` / `student_num` 必填。其余两项决定了这个账号能不能自愈：

| 字段 | 缺了会怎样 |
|---|---|
| `refresh_token` | token 失效即失败，需重新抓包。**强烈建议配置** |
| `face_file` | `face_verify_id` 失效即失败，需重新抓包 |

`face_file` 是**人脸照片在 OSS 上的路径**，真机拍照核身时 App 先把它传上去，
再拿这个路径去调核验接口。路径格式
`Public/Upload/pic/face_verify/<日期>/<三位数>/<uuid>.jpg`。

未知字段会被拒绝 —— `tokne` 这类拼写错误在宽松解析下会静默失效。
文件权限 `600`。

### 4.2 跑步参数 `config.json`

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
每次运行现取。

---

## 5 · 提交前的强制要求

四条，每条都附证据。

### 5.1 `log_data[].distance` 必须各点同值

服务端按「相邻点距离差 ÷ 时间差」做按段测速。填累计里程会让首段算出
20.8 km/h 这类不可能的瞬时速度而被拒；各点同值则相邻差为 0，服务端算不出速度。

真机抓包里的有效记录本就如此（四个点该字段是同一个值）。

> 这条规则同时扮演反作弊与反伪造两种角色：真实客户端的 GPS 自然产生等距段，
> 不会触发；按「累计里程」直觉填值的伪造者反而会暴露。

### 5.2 跑步窗口内必须铺实时上报

`beforeRunV260` 的 `run_check` plan 1 要求：

```json
{"plan": 1, "inteval_time": 30, "check_num": 5, "is_stop": 0}
```

**缺上报直接判「跑步时间异常」**（`record_id=30461`）。加上后即通过
（`record_id=30542`、`30606`）。

而且上报必须**从窗口开头就铺开**，理由见 §6。

### 5.3 必须走完人脸核验三步

见 §3.5。缺 `type=2` 判「人脸验证不合格」（`record_id=30598`）。

### 5.4 `run_check_res` 字段

客户端把**自己测到的**结果回传，一条检查计划对应一个条目：

| plan | `inteval_value` 格式 | 含义 |
|---|---|---|
| 1 | `0-<km/h>,0-<km/h>,…` | 每 30 秒实测配速 |
| 4 | 全 `0` | 每 100m 漂移 |
| 5 | `<步数>-0,<步数>-0,…` | 每 100m 步数 |
| 6 | 全 `0` | 每 100m 步数漂移 |

条数按 `used_time // 30` 取。**与实际上报次数无关** —— 真机样本
`used_time=575` 下发 19 条，却只上报 4 次，也照样通过。

`is_cheat` / `is_stop` 恒为 `0`。

---

## 6 · 时序模型

### 6.1 为什么每个账号必须等 7 分钟

三条硬约束叠出来的结果，不是保守设计：

1. **跑步窗口下限 288 秒。** `used_time = 距离 × 配速`，配速合法区间
   180–540 秒/公里，最短 1.60 km 配 180 秒/公里就是 288 秒。窗口短不了。
2. **实时上报必须真实等待。** 服务端按请求到达的真实时刻记录，无法回拨。
3. **上报要从窗口开头铺开。** 这一点是踩出来的：

| 方案 | 首次上报落在窗口 | 结果 |
|---|---|---|
| 窗口整体往回拨，上报连发 | 99% | ❌ `record_id=30607` |
| 窗口整体往回拨，上报 30 秒间隔 | 68% | ❌ `record_id=30621` |
| 窗口向前铺开，上报 60 秒间隔 | 5% | ✅ `record_id=30542` |

**整体回拨窗口虽然能免掉等待，但必然把上报挤到末端。**

外加 `end_time` 必须严格早于提交时刻 —— 零延迟完成会被拒。

于是：窗口 420 秒，上报 6×60 秒铺满前 300 秒，末次上报后再等 120 秒提交。

### 6.2 时序参数的约束关系

```
T0                       首次上报时刻
reports_end = T0 + (report_count-1) × report_interval
end_time    = reports_end + end_pad          必须晚于末次上报
submit_at   = reports_end + submit_tail       必须晚于 end_time
start_time  = end_time - used_time - end_margin
```

**`end_pad` 必须小于 `submit_tail`。** 否则 `end_time` 落到未来，
触发零延迟拒绝 —— 而提示是「跑步时间异常」，极具误导性（踩过，`record_id=30617`）。

`Config.validate()` 在发请求前挡住这几种配错：

- `end_pad >= submit_tail`
- `report_interval = 0`（连发）
- 上报跨度 ≥ 整个窗口

### 6.3 有效时段

服务端只认**当天** 06:00–20:30 上传的成绩。窗口外直接失败而不回填时间。
`check_window()` 本地预判，不发请求。

---

## 7 · 凭据生命周期

### 7.1 两条铁律

1. **续期要求 `(access_token, refresh_token)` 是当前活跃的那一对。**
   续期后旧的一对**立即作废**，重复使用旧对得到 `101`。

   > 推论：**重复续期会连锁作废。** 新拿到的那对若此时还没落盘就被下一次续期顶掉，
   > 最终什么都没剩下。`_renewed` 标志就是防这个的。

2. **返回值必须立刻落盘。** 丢弃等于烧掉凭据，只能回真机重新登录。

`face_verify_id` 同样会被服务端作废（过期、或对应的跑步被放弃），
新值也要立刻写回。

### 7.2 写回

```python
staged = f"{source}.tmp"
写临时文件 → chmod 600 → os.replace(staged, source)
```

**`os.replace` 失败时，暂存文件里存着可用副本，必须保留并报出位置。**
删掉它等于把凭据永久销毁。

值没变化则跳过写入，避免无谓的磁盘操作。

### 7.3 通道绑定

按 `docs.md` §9.1，经信封续期签发的 token 只能用于信封请求，用于明文表单
（`ostype=2`）会得到 `101`。本脚本全程走信封，不受影响。

> ⚠️ 该结论中「App 登录签发的 token 不能用于信封通道」一条**已被实测推翻** ——
> App 登录签发的 token 在信封通道上正常工作。见 §10。

---

## 8 · 错误语义与伪装

**这是排障时最需要的一张表。** 同一句提示可能对应完全不同的成因，
没有这张表只能靠猜。

| 症状 | 可能的成因 | 怎么区分 |
|---|---|---|
| `跑步时间异常` | ① 完全没有实时上报<br>② 上报挤在窗口末端<br>③ `end_time` 落在未来<br>④ 四个必为字符串的字段传了 float | 逐条对照 §5 / §6 |
| `当天关联成绩次数已达到上限` | **配额检查在时间检查之前** | 此时看不到时间检查结果，账号配置是否有问题**无法判断**，改用 `--check` |
| `登录信息失效，请重新登录` | token 自然过期 **或** 同一对凭据被重复续期 | 续期后仍失败 → 是后者，需重新抓包 |
| `人脸验证不合格` | 只做了 `type=0` 未走 `type=2`<br>或 `face_file` 缺失 | 检查 `face_file` 是否配置 |
| `非法请求` | 签名算错（见 §2.2）<br>或四个字段类型错 | 先验证签名 |
| `未返回 data` | 响应结构不符预期 | 部分接口（`getStudentVenaPoint`）本就没有 `data`，要用 `_accept` |
| OSS `bucket acl` 拒绝 | PUT 到了对象路径 | 必须 POST 到 bucket 根路径 |

**退出前的最后一个错误往往不是根因。** 配额满会把真实状态掩盖掉。

---

## 9 · 被证伪的假设

这几条都是花时间试出来的，写下来省得重试。

### 9.1 「`faceVerify` 需要跑步层私有盐，无法伪造」

**错。** 在小程序信封下直接可调用，`type=0` 与 `type=2` 全通，返回明文
`run_face_verify_id`。`docs.md` §2.2 把私有盐的覆盖范围列为包含该接口，
这条已被实测推翻。

### 9.2 「`setRunLocationRecord` 需要私有盐」

**错。** 同上，可直接调用，且 `lp31` 非必需。

### 9.3 「拿到 `type=0` 的 id 就能提交」

**错。** 只做 `type=0` 会被判「人脸验证不合格」（`record_id=30598`）。
必须走完 `type=2`。

### 9.4 「窗口整体回拨就能免掉等待」

**错。** 等待确实免掉了，但上报被挤到窗口末端，必被拒（§6.1）。

### 9.5 「上报间隔可以压到 30 秒甚至连发」

**错。** 30 秒间隔只覆盖窗口 36%，连发则是 0% —— 两种都被拒。

### 9.6 「App 登录签发的 token 不能用于小程序信封」

**存疑。** 实测 App 签发的 token 在信封通道上正常工作，与 `docs.md` §9.1
的表格相反。该表可能基于另一个样本，或只对特定签发路径成立。

---

## 10 · 已知缺口

### 10.1 `stopRunV278` 仍缺 8 个字段

真机请求 38 个字段，脚本 33 个。缺的：

| 字段 | 说明 |
|---|---|
| `lp31` | 反作弊载荷，需私有盐，无法构造 |
| `record_str` | 32 位十六进制，疑似摘要。已试 `md5(log_data)`、`md5(log_data+token)`、`md5(uid+student_num)`、`md5(start+end)`、`md5(record_file)`、`md5(uid)`、`md5(token)` **全部不中** |
| `pass_effective_num` / `pass_total` | 真机是 104/104 与 409/409，含义不明（推测为有效成绩计数），猜错可能比不填更糟 |
| `ip` / `mac` / `gee_token` / `newMobileDeviceId` | 设备信息 |

当前 33 个字段已能提交出 `record_status=1` 的有效成绩，这些缺口暂未构成阻碍。

### 10.2 私有盐

仍是唯一的根本未知。`docs.md` §9.2 提了 iOS 侧用 chained fixups 定位的思路。

### 10.3 服务端行为的时间分界

2026-10-16 上午 09:06 用当时的脚本参数能提交成功（`record_id=30288` 那一批），
同日 12:40 起对所有账号全量失败。加上了实时上报与人脸三步之后恢复。

两种解释无法区分：服务端当天中途收紧了校验 / 账号被真机「武装」后才收紧。
区分实验需要配额重置后再验。

---

## 11 · 排障流程

```
1. python3 selftest.py          代码不变量，秒级。失败说明改坏了
2. python3 runner.py --check    账号自愈能力。要打一次网络
     → 报「无 refresh_token」/「无 face_file」/「token 已失效」时，
       对应补齐后重跑，不要往参数方向查
3. python3 runner.py --dry-run  按真实时序演练，看窗口与上报落点是否合理
4. 在控制台输出里筛该账号的记录（`grep '\[lzy\]'` 或 `python3 runner.py lzy`）
5. 对照 §8 的错误表定位成因
```

**配额已满的账号看不到时间检查结果** —— 此时 `--check` 是唯一可靠手段。

日志只输出到控制台（stderr），**不写任何文件**，需要留档由 shell 重定向
（`python3 runner.py > /dev/null 2>> run.log`）。

DEBUG 级别记录**解密后的明文** —— 密文既不可读，按字段名匹配的脱敏规则也作用不上。
其中 `token` / `access_token` / `refresh_token` / `run_face_verify_id` / `sign`
会被替换成首尾片段：终端回滚缓冲、tmux、录屏都可能把明文带出去，脱敏仍然必要。

---

## 12 · 改动检查清单

**改完代码先跑 `selftest.py`。**

其中最要紧的一条：扫描 `lptiyu.py` 里所有 `identity[...]` 读取，校验
`Account.identity()` 都提供了对应字段。**漏字段不会在加载时暴露，而是跑到
那一步才 `KeyError`** —— 实测因此白等过 7 分钟（`renew_face_id` 读 `school_id`、
`submit_run` 读 `face_verify_id`，各中过一次）。

其它几条各守一个踩过的坑：签名形式、时序预检、段值同值、配速字段反直觉、
明文 data 响应、配额错误分类、写回保活。

改动时序相关参数前必读 §6；改动任何一处签名或加密前必读 §2。