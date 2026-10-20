# garmin-sync-szxd

把 **佳明 Garmin Connect** 的历史运动记录批量导入 **数字心动 App** 的小工具。

数字心动官方只提供网页版「运动数据同步」（NRC 迁移通道），一次只能传一个文件、不接受 FIT、也不处理时区。这个脚本把这些坑都封好了：

- 从佳明批量导出 TCX（官方导出接口，保留轨迹 / 心率 / 步频 / 分段）
- 自动把时间整体平移到本地时区（数字心动把 UTC 当本地时间入库，不处理会整体早 8 小时）
- 批量上传并按「开始时间 + 距离」判重，重复的不再上传
- 上传前可 --dry-run 预演，只统计不写数据

## 先说清楚限制

| 项目 | 实际情况 |
| --- | --- |
| 支持的运动类型 | **只有跑步**。平台只挂了 businessId=1 / subBusinessId=11 的解析队列，骑行、步行、力量传上去接口返回 code:0，但永远停在「处理中」，不会入库 |
| 删除 | **服务端没有删除接口**，删除只能在 App 里单条操作，传错了得手动删 |
| 账号 | 只能导自己账号，需要 App 里的短信验证码登录 |
| 数据量 | 单文件 2-6 MB、几百条记录实测没问题 |

## 安装

```bash
git clone https://github.com/yiumanlenong/garmin-sync-szxd.git
cd garmin-sync-szxd
pip install garth
```

Python 3.9+。`garth` 负责和佳明通信（免 MFA 的 token 登录）。

首次使用先登录佳明，token 会存在 `~/.garminconnect`：

```bash
python -m garth login --domain garmin.cn
```

国区用 `garmin.cn`，国际区用 `garmin.com`。登录一次可长期复用，脚本会自动续期。

## 三步使用

### 1. 拿数字心动的 token

打开手机上的数字心动 App，在登录页输入手机号并点「获取验证码」，**收到短信后不要点登录**，直接把 6 位码传给脚本：

```bash
python sync.py login --phone 138xxxxxxxx --code 123456
```

成功后会打印 `accountId`，token 存到 `~/.szxd_token`。

> 为什么要绕这一圈：网页的发码接口强制腾讯云 TCaptcha 人机校验（返回 `1004015 人机校验参数缺失`），脚本发不出短信；而 App 的验证码可以直接在接口侧换 token。参数必须是 `captchaType=2` + `loginType=1`，其它组合会报「验证码错误」。

### 2. 从佳明导出

```bash
python sync.py export --out tcx --types running --tz 8
```

- `--out` 输出目录，文件名形如 `20260420_running_586372558.tcx`（日期_类型_活动ID）
- `--types` 活动类型，逗号分隔，默认只导 `running`。`road_biking` 也能导出，但传不进数字心动
- `--tz 8` 北京时间偏移。导出的 TCX 里所有时间戳整体 +8 小时并保留 `Z` 后缀，这正是数字心动需要的格式

导出走佳明官方 `download-service/export/tcx`，和网页上「导出为 TCX」是同一份数据。

### 3. 上传

先预演，看清会传几条、跳过几条：

```bash
python sync.py upload --dir tcx --dry-run
```

确认后正式传：

```bash
python sync.py upload --dir tcx
```

判重规则（命中任意一条就跳过）：

1. 本地台账 `tcx/.szxd-uploaded.json` 里已有同名文件，防止脚本重跑重复上传
2. 平台已有记录中，开始时间相差不超过 60 秒且距离差小于 10 米
3. 开始时间正好相差 `--tz` 小时且距离一致，拦住「同一活动曾以 UTC 时间入过库」的情况

上传成功的文件会立刻写进台账，中断了可以重跑，不会重复。

## 参数速查

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| login | `--phone` / `--code` | App 里获取的短信验证码 |
| export | `--out` | 输出目录，默认 `tcx` |
|  | `--types` | 活动类型，默认 `running` |
|  | `--tz` | 时区偏移，默认 8 |
|  | `--garmin-domain` | `garmin.cn`（默认）或 `garmin.com` |
| upload | `--dir` | TCX 目录 |
|  | `--osid` | 请求头 osId，默认 `1002` |
|  | `--delay` | 每个文件间隔秒数，默认 1.5 |
|  | `--dry-run` | 只预演，不上传 |

## 常见报错

| 返回 | 原因 | 处理 |
| --- | --- | --- |
| `1004081 所属系统不能为空` | 请求头 osId 用了 1010 | 用默认的 `1002` |
| `1004011 Token为空` | token 没带上或已过期 | 重新跑 `login` |
| `1011004 验证码错误` | 码过期，或用了网页场景的码 | 重新在 App 取码，1 分钟内用掉 |
| `1004015 人机校验参数缺失` | 直接调了发码接口 | 只能从 App 拿码 |
| 文件一直「处理中」 | subBusinessId 不是 11 | 只能传跑步，用默认 11 |

## 工作原理

1. **导出**：`GET /download-service/export/tcx/activity/{id}?full=true`，用 garth 的会话请求
2. **修时区**：正则替换 TCX 里所有 `YYYY-MM-DDTHH:MM:SS(.sss)Z`，整体加 `--tz` 小时
3. **上传**：`POST /changzheng-sport-proxy-api/api/sport/data/import/admin/import`，multipart 字段 `file` + `businessId=1` + `subBusinessId=11`，请求头带 `token`、`osId: 1002`、`terminalType: 3`
4. **判重**：`POST .../admin/data/page` 拉已有记录，比对开始时间和 `realDistance`

## 免责声明

- 仅用于同步**你自己账号**的**真实运动数据**。
- 伪造运动数据是平台明令禁止的行为（上传页原文：「一经发现…由上传者负责」）。本工具不提供修改距离、时长或造假时间的功能，`--tz` 只做时区校正。
- 平台接口随时可能变更，脚本按 2026-10 的接口实现，失效请提 issue。
