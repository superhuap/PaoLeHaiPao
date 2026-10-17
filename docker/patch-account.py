#!/usr/bin/env python3
"""只改账号 JSON 里指定的那几个字段。

为什么需要它：accounts/ 是宿主机的 bind-mount，容器里 runner 续期后会就地
写回 token。手改 JSON 很容易连带把 token 写坏 —— 凭据一旦被覆盖，旧的
(token, refresh_token) 对已经作废，账号就彻底失联，只能回手机抓包。

    python3 docker/patch-account.py cyh \
        --face-file Public/Upload/pic/face_verify/2026-10-17/396/xxx.jpg \
        --face-id 6ad2d2d1040d1

只动命令行显式给出的字段，其余一个字节都不碰。写前会校验 token 仍在，
写后原子替换（临时文件 + os.replace），并按 0600 落盘。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS = os.path.join(os.path.dirname(HERE), "accounts")

# 打印时打码，避免部署日志里流出完整凭据
SECRET = {"token", "refresh_token"}


def mask(value: str) -> str:
    return f"{value[:8]}…" if len(value) > 12 else "<空>"


def main() -> int:
    parser = argparse.ArgumentParser(description="只改账号 JSON 的指定字段")
    parser.add_argument("name", help="账号名，对应 accounts/<name>.json")
    parser.add_argument("--face-file", help="人脸照片的 OSS 路径")
    parser.add_argument("--face-id", help="run_face_verify_id")
    parser.add_argument("--accounts-dir", default=ACCOUNTS,
                        help="账号目录，默认取仓库内 accounts/")
    args = parser.parse_args()

    updates = {}
    if args.face_file:
        updates["face_file"] = args.face_file
    if args.face_id:
        updates["face_verify_id"] = args.face_id
    if not updates:
        parser.error("至少要给一个要改的字段")

    path = os.path.join(args.accounts_dir, f"{args.name}.json")
    try:
        with open(path, encoding="utf-8") as handle:
            account = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"读不到 {path}：{exc}", file=sys.stderr)
        return 2

    if not account.get("token"):
        print(f"{path} 里没有 token，先修好账号再改其它字段", file=sys.stderr)
        return 2

    before = {key: account.get(key) for key in updates}
    changed = [key for key, value in updates.items() if account.get(key) != value]
    if not changed:
        print(f"{args.name} 已经是目标值，无需改动")
        return 0

    account.update(updates)

    # 凭据必须原样留着 —— 这是这个脚本存在的全部理由
    for key in SECRET:
        if not account.get(key):
            print(f"改写后 {key} 丢了，已中止", file=sys.stderr)
            return 2

    staged = f"{path}.tmp"
    try:
        with open(staged, "w", encoding="utf-8") as handle:
            json.dump(account, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(staged, 0o600)
        os.replace(staged, path)
    except OSError as exc:
        print(f"写入失败：{exc}", file=sys.stderr)
        print(f"暂存文件留在 {staged}，原文件未动", file=sys.stderr)
        return 1

    for key in changed:
        old = mask(str(before[key])) if key in SECRET else before[key]
        new = mask(str(account[key])) if key in SECRET else account[key]
        print(f"  {args.name}.{key}: {old}  →  {new}")
    print(f"{path} 已更新（{len(changed)} 个字段，token 未动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
