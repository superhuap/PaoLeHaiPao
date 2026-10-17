#!/bin/sh
# 容器入口：准备 /data，然后在「常驻定时」和「一次性执行」之间分流。
#
#   schedule（默认）  常驻，每天在 SCHEDULE_TIMES 指定的时刻跑一次
#   run-once          立刻跑一轮然后退出
#   check             只体检账号，不发请求
#   refresh           只刷新凭据（token 续期并写回 JSON）
#   refresh-face      同上，附带重做人脸核验
#   dry-run           按真实时序演练，不发请求
#   <任意其他参数>     原样透传给 runner.py
#
# 数据持久化：/data 是卷，entrypoint 只在文件「不存在」时写入默认内容，
# 已存在的 config.json / accounts/*.json 一律不碰 —— runner.py 续期后的
# token 就地写回这些文件。
set -eu

APP_DIR="${APP_DIR:-/app}"
DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "${DATA_DIR}/accounts" "${DATA_DIR}/logs" "${DATA_DIR}/state"

if [ ! -f "${DATA_DIR}/config.json" ]; then
    cp "${APP_DIR}/seed-config.json" "${DATA_DIR}/config.json"
    echo "[entrypoint] 已生成默认 ${DATA_DIR}/config.json"
fi

for template in "${APP_DIR}/seed-accounts/"*.example.json; do
    [ -e "${template}" ] || continue
    # 落地成 .disabled：runner.py 只扫 *.json，模板放着不会被当成账号去加载报错
    target="${DATA_DIR}/accounts/$(basename "${template}" .example.json).json.disabled"
    if [ ! -f "${target}" ]; then
        cp "${template}" "${target}"
        echo "[entrypoint] 已生成账号模板 ${target}"
    fi
done

total=$(ls -1 "${DATA_DIR}"/accounts/*.json 2>/dev/null | wc -l | tr -d ' ')
if [ "${total:-0}" = "0" ]; then
    echo "[entrypoint] ${DATA_DIR}/accounts 里没有账号文件。"
    echo "[entrypoint] 改名去掉 .disabled 并把 enabled 改成 true 即可。"
    echo "[entrypoint] 照 ${DATA_DIR}/accounts/example.json.disabled 填好凭据，"
else
    ready=$(grep -l '"refresh_token": *"[^"]\+"' "${DATA_DIR}"/accounts/*.json 2>/dev/null | wc -l | tr -d ' ')
    echo "[entrypoint] 账号文件 ${total} 个，其中带凭据可跑的 ${ready} 个"
fi

# 时区体检：本项目用 time.localtime() 判当天有效时段（lptiyu.py:610），
# 本地时区一旦悄悄回落 UTC，跑步时段会整体判错且报错信息完全看不出原因，
# 所以启动就把实际偏移打出来，不对就喊出来。
python - <<'PY' || true
import datetime, os, time
offset = -time.timezone // 3600
stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
print(f"[entrypoint] 时区 TZ={os.environ.get('TZ', '(未设)')} · 实际 UTC+{offset} · 本地 {stamp}")
expected = os.environ.get("TZ_EXPECT_OFFSET", "8")
if offset != int(expected):
    print(f"[entrypoint] 警告：本地时区是 UTC+{offset}，与预期 UTC+{expected} 不符，"
          "当天有效时段会判错。检查镜像里 /usr/share/zoneinfo 是否可用。")
PY

case "${1:-schedule}" in
    schedule)   exec python "${APP_DIR}/scheduler.py" ;;
    run-once)   shift; exec python "${APP_DIR}/runner.py" "$@" ;;
    check)      shift; exec python "${APP_DIR}/runner.py" --check "$@" ;;
    refresh)    shift; exec python "${APP_DIR}/runner.py" --refresh "$@" ;;
    refresh-face) shift; exec python "${APP_DIR}/runner.py" --refresh --refresh-face "$@" ;;
    dry-run)    shift; exec python "${APP_DIR}/runner.py" --dry-run "$@" ;;
    selftest)   exec python "${APP_DIR}/selftest.py" ;;
    shell)      exec /bin/sh ;;
    *)          exec python "${APP_DIR}/runner.py" "$@" ;;
esac