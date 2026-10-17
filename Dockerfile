# 步道乐跑调度器镜像
#
# 数据全部落在 /data（config.json + accounts/*.json + logs/），
# 容器里 /app/config.json 和 /app/accounts 是指向 /data 的软链，
# 所以 token 续期写回时直接落在挂载出来的宿主文件上 —— 原子写（临时文件 +
# os.replace）也照旧生效。代码在镜像层，删容器不丢数据。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data \
    SCHEDULE_TIMES=07:30,13:30 \
    RUN_ARGS= \
    RUN_ON_START=0

# 刻意不装任何系统包：不 apt-get update、不装 tzdata，镜像更小、构建不依赖
# apt 源。代价是 Debian slim 没有 /usr/share/zoneinfo，glibc 的 time.localtime()
# 会静默回落 UTC —— 而本项目用本地时区判当天有效时段（lptiyu.py:610）、
# 生成 OSS 对象 key（lptiyu.py:774），回落 UTC 会让时段判定全错且报错看不出原因。
WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# 把 pip tzdata 包里的 TZif 目录链成系统 zoneinfo 根目录：C 库
#（time.localtime）与 Python（zoneinfo / datetime）读同一份数据。
RUN ZONEINFO="$(python -c 'import tzdata,os;print(os.path.join(os.path.dirname(tzdata.__file__),"zoneinfo"))')" \
 && test -f "${ZONEINFO}/Asia/Shanghai" \
 && if [ -d /usr/share/zoneinfo ] && [ -n "$(ls -A /usr/share/zoneinfo 2>/dev/null)" ]; then \
        echo "基础镜像自带 zoneinfo，保持不动"; \
    else \
        ln -sfn "${ZONEINFO}" /usr/share/zoneinfo && echo "zoneinfo → ${ZONEINFO}"; \
    fi \
 && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
 && echo "${TZ}" > /etc/timezone \
 && TZ=Asia/Shanghai date \
 && python -c 'import time,datetime;print("构建期时区自检：本地 UTC+%d · %s" % (-time.timezone//3600, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))'

COPY runner.py lptiyu.py logs.py selftest.py ./
COPY config.json /app/seed-config.json
COPY docker/entrypoint.sh docker/scheduler.py /app/
COPY docker/seed/accounts/ /app/seed-accounts/

# 首次启动时的空数据盘：默认配置 + 账号模板（真实账号不进镜像）。
# 种子配置必须在这里落成真文件：/app/config.json 是指向它的软链，而
# selftest.py 的 TestTimeline 直接读它 —— 构建期文件缺失会让自检以
# FileNotFoundError 挂掉，测的并不是代码而是没喂进去的数据。
RUN mkdir -p /data/accounts /data/logs /data/state \
 && chmod +x /app/entrypoint.sh /app/scheduler.py \
 && cp /app/seed-config.json /data/config.json \
 && python -c 'import json;json.load(open("/data/config.json"));print("种子配置就位且可解析")'

# 数据面软链：代码只认 /app 下的相对路径，数据实际存 /data
RUN ln -s /data/config.json /app/config.json \
 && ln -s /data/accounts /app/accounts

# 构建期自检：selftest 不打网络，坏了就别出镜像（秒级）
RUN python selftest.py

VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import os,sys,time;p='/data/state/heartbeat';sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<300 else 1)"

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["schedule"]