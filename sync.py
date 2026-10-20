#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""佳明（Garmin Connect）→ 数字心动 历史运动数据同步工具。

三步：login 拿 token → export 导出 TCX（自动修时区）→ upload 上传（自动判重）。

只做跑步数据：数字心动的导入通道只挂了跑步解析队列，
骑行/步行等其它类型即使上传也不会入库（详见 README）。
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

SZ_BASE = "https://api-changzheng.chinaath.com"
SZ_LOGIN = SZ_BASE + "/changzheng-user-center-api/login"
SZ_ADMIN = SZ_BASE + "/changzheng-sport-proxy-api/api/sport/data/import/admin"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
HOME = os.path.expanduser("~")
TOKEN_FILE = os.path.join(HOME, ".szxd_token")
LEDGER_NAME = ".szxd-uploaded.json"

TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?Z")


# ---------------------------------------------------------------- HTTP helpers
def _req(url, data=None, headers=None, timeout=180, method=None):
    h = {"User-Agent": UA, "Origin": "https://race.shuzixindong.com",
         "Referer": "https://race.shuzixindong.com/"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return {"raw": raw[:300].decode("utf-8", "ignore")}


def sz_headers(token, osid="1002"):
    return {"token": token, "terminalType": "3", "osId": str(osid), "userId": "0",
            "machineCode": str(int(time.time() * 1000)), "Content-Type": "application/json"}


def account_id(token: str) -> str:
    payload = token.split(".")[1]
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return str(data.get("accountId") or data.get("userId"))


# -------------------------------------------------------------------- 登录
def cmd_login(args):
    """用手机号 + 短信验证码换取 token。

    验证码必须在数字心动 App 的登录页点「获取验证码」拿到：
    网页的发码接口强制腾讯云人机校验，脚本发不了码。
    拿到码后不要在 App 里点登录，直接传给本命令。
    """
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    parts = [("captchaType", "2"), ("phone", args.phone), ("captcha", args.code), ("loginType", "1")]
    body = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                   for k, v in parts) + f"--{boundary}--\r\n"
    st, raw = _req(SZ_LOGIN, body.encode(),
                   {"Content-Type": f"multipart/form-data; boundary={boundary}",
                    "terminalType": "3", "osId": "1010", "userId": "0",
                    "machineCode": str(int(time.time() * 1000))})
    j = _json(raw)
    token = ((j.get("data") or {}).get("token")) if isinstance(j, dict) else None
    if not token:
        sys.exit(f"登录失败：{j.get('code')} {j.get('msg') or j}")
    open(TOKEN_FILE, "w").write(token)
    print(f"登录成功，token 已保存到 {TOKEN_FILE}（accountId={account_id(token)}）")


def load_token() -> str:
    if not os.path.exists(TOKEN_FILE):
        sys.exit("没有 token，先运行：python sync.py login --phone 手机号 --code 验证码")
    return open(TOKEN_FILE).read().strip()


# -------------------------------------------------------------------- 导出
def shift_timestamps(text: str, hours: int) -> str:
    """把 TCX 里所有 UTC 时间整体平移。

    数字心动把 <Id> 里的时间当本地时间直接入库，不做时区换算，
    所以必须先把时间改成"本地时间 + Z"，否则记录会整体早 8 小时。
    """
    if not hours:
        return text

    def _sub(m):
        y, mo, d, h, mi, s, frac = m.groups()
        t = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s)) + dt.timedelta(hours=hours)
        return t.strftime("%Y-%m-%dT%H:%M:%S") + (frac or "") + "Z"

    return TS_RE.sub(_sub, text)


def cmd_export(args):
    try:
        import garth
    except ImportError:
        sys.exit("缺少依赖：pip install garth")

    garth.configure(domain=args.garmin_domain)
    garth.resume(os.path.join(HOME, ".garminconnect"))
    client = garth.client

    acts = client.connectapi("/activitylist-service/activities/search/activities?start=0&limit=1000")
    want = set(args.types.split(","))
    sel = [a for a in acts if a["activityType"]["typeKey"] in want]
    sel.sort(key=lambda a: a["startTimeLocal"])
    os.makedirs(args.out, exist_ok=True)

    ok = fail = 0
    for i, a in enumerate(sel, 1):
        aid = a["activityId"]
        name = f'{a["startTimeLocal"][:10].replace("-", "")}_{a["activityType"]["typeKey"]}_{aid}.tcx'
        path = os.path.join(args.out, name)
        if os.path.exists(path) and os.path.getsize(path) > 2000:
            ok += 1
            continue
        body = None
        for q in ("?full=true", ""):
            try:
                body = client.download(f"/download-service/export/tcx/activity/{aid}{q}")
                if body and b"<Trackpoint>" in body:
                    break
                body = None
            except Exception:
                body = None
        if not body:
            fail += 1
            print(f"  跳过 {aid}（导出失败）")
            continue
        text = shift_timestamps(body.decode("utf-8", "ignore"), args.tz)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(text)
        ok += 1
        if i % 10 == 0:
            print(f"  {i}/{len(sel)}", flush=True)
    print(f"导出完成：{ok} 个 TCX → {args.out}（失败 {fail}）")


# -------------------------------------------------------------------- 上传
def fetch_records(token: str, acc: str, biz=1, sid=11):
    rows, page = [], 1
    while True:
        body = json.dumps({"accountId": acc, "businessId": biz, "subBusinessId": sid,
                           "pageNo": page, "pageSize": 100}).encode()
        st, raw = _req(SZ_ADMIN + "/data/page", body, sz_headers(token))
        j = _json(raw)
        batch = (j.get("data") or {}).get("results") or []
        rows += batch
        if len(batch) < 100:
            return rows
        page += 1


def upload_one(token: str, path: str, biz=1, sid=11, osid="1002"):
    content = open(path, "rb").read()
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="businessId"\r\n\r\n{biz}\r\n'.encode(),
             f'--{boundary}\r\nContent-Disposition: form-data; name="subBusinessId"\r\n\r\n{sid}\r\n'.encode(),
             (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{os.path.basename(path)}"\r\n'
              f'Content-Type: application/octet-stream\r\n\r\n').encode() + content + b"\r\n",
             f"--{boundary}--\r\n".encode()]
    h = sz_headers(token, osid)
    h["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    st, raw = _req(SZ_ADMIN + "/import", b"".join(parts), h)
    return _json(raw)


def tcx_meta(path: str):
    """从 TCX 里取出开始时间和总里程（各 Lap 的 DistanceMeters 之和）。"""
    text = io.open(path, encoding="utf-8").read()
    m = re.search(r"<Id>([^<]+)</Id>", text)
    if not m:
        return None, 0.0
    start = m.group(1)[:19].replace("T", " ")
    total = 0.0
    for lap in re.findall(r"<Lap\b.*?</Lap>", text, re.S):
        d = re.search(r"<DistanceMeters>([\d.]+)</DistanceMeters>", lap)
        if d:
            total += float(d.group(1))
    return start, total


def cmd_upload(args):
    token = load_token()
    acc = account_id(token)
    records = fetch_records(token, acc)
    existing = [(r["runStartTime"], float(r.get("realDistance") or 0))
                for r in records if r.get("runStartTime")]
    print(f"平台上已有 {len(existing)} 条记录（含历史导入日志）")

    ledger_path = os.path.join(args.dir, LEDGER_NAME)
    ledger = json.load(open(ledger_path, encoding="utf-8")) if os.path.exists(ledger_path) else {"done": []}
    done = set(ledger.get("done", []))

    files = sorted(f for f in os.listdir(args.dir) if f.endswith(".tcx"))
    stats = {"skip_ledger": 0, "skip_dup": 0, "ok": 0, "fail": 0}
    for i, name in enumerate(files, 1):
        path = os.path.join(args.dir, name)
        if name in done:
            stats["skip_ledger"] += 1
            continue
        start, dist = tcx_meta(path)
        if start and is_duplicate(start, dist, existing, args.tz):
            stats["skip_dup"] += 1
            print(f"  跳过（已存在）{name} @ {start}")
            continue
        if args.dry_run:
            stats["ok"] += 1
            print(f"  [预演] 将上传 {name} @ {start} {dist / 1000:.2f}km")
            continue
        r = upload_one(token, path, osid=args.osid)
        if r.get("code") == 0:
            stats["ok"] += 1
            done.add(name)
            json.dump({"done": sorted(done)}, open(ledger_path, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        else:
            stats["fail"] += 1
            print(f"  失败 {name}：{r.get('code')} {r.get('msg')}")
        if i % 10 == 0:
            print(f"  {i}/{len(files)}", flush=True)
        time.sleep(args.delay)
    print(f"完成：上传 {stats['ok']}，跳过重复 {stats['skip_dup']}，"
          f"跳过台账 {stats['skip_ledger']}，失败 {stats['fail']}")


def is_duplicate(start: str, dist: float, existing, tz_hours: int = 0) -> bool:
    """同一开始时间（±60 秒）且距离差 <10 米视为重复。

    另外把"时间正好差 tz 小时 + 距离一致"也算重复，用于拦住
    早先以 UTC 时间入过库的同一活动。
    """
    t = dt.datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    for s, d in existing:
        try:
            other = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        gap = abs((other - t).total_seconds())
        if abs(d - dist) < 10 and (gap <= 60 or abs(gap - abs(tz_hours) * 3600) <= 60):
            return True
    return False


# -------------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(description="佳明 → 数字心动 历史运动数据同步")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("login", help="用手机号 + App 短信验证码换取 token")
    a.add_argument("--phone", required=True)
    a.add_argument("--code", required=True)
    a.set_defaults(func=cmd_login)

    e = sub.add_parser("export", help="从佳明批量导出 TCX 并修正时区")
    e.add_argument("--out", default="tcx", help="输出目录")
    e.add_argument("--types", default="running",
                   help="活动类型，逗号分隔（running,road_biking…）。默认 running")
    e.add_argument("--tz", type=int, default=8, help="时区偏移小时数，默认 8（北京时间）")
    e.add_argument("--garmin-domain", default="garmin.cn", help="国区 garmin.cn / 国际 garmin.com")
    e.set_defaults(func=cmd_export)

    u = sub.add_parser("upload", help="批量上传 TCX 到数字心动（带判重）")
    u.add_argument("--dir", default="tcx")
    u.add_argument("--osid", default="1002", help="请求头 osId，默认 1002（用 1010 会报“所属系统不能为空”）")
    u.add_argument("--tz", type=int, default=8)
    u.add_argument("--delay", type=float, default=1.5, help="每个文件之间的间隔秒数")
    u.add_argument("--dry-run", action="store_true", help="只预演判重，不实际上传")
    u.set_defaults(func=cmd_upload)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
