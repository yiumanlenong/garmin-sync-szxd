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

首次使用先登录佳明，token 会存到 `~/.garminconnect`：

```bash
python sync.py garmin-login --domain garmin.cn
```

按提示输入佳明邮箱/手机号和密码；开了两步验证会再要一次 6 位码。国区用 `garmin.cn`，国际区用 `garmin.com`。登录一次可长期复用，脚本会自动续期。

> 不要用 `python -m garth login`：garth 0.4.40 之后删掉了命令行入口，会报 `No module named garth.__main__`。

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

## 判重逻辑

两层，命中任意一层就跳过：

1. **本地台账** `tcx/.szxd-uploaded.json`：这个目录里哪些文件已经**解析成功**过，重跑不会重复传。上传后核对失败的会自动从台账摘除，下次还会重试。想全部重来就直接删掉这个文件
2. **平台流水表**：开始时间相差 ≤60 秒且距离差 <10 米，或时间正好差 `--tz` 小时且距离一致

### 在 App 里删了记录，为什么还判重？

平台有两道去重，第二道是服务端硬限制：

1. **客户端**（本脚本）：比对导入流水表，命中就跳过。这张表只增不减，App 删记录不会清理它
2. **服务端**：按开始时间去重。App 里的删除是软删，不释放这个键

实测结果：删掉 `2026-10-15 19:43:54` 这条后重新上传，文件被接收（`code:0`），但异步解析返回 `status=2`、`运动数据已被同步过`。把同一文件的时间整体挪 60 秒再传，就 `status=1` 成功入库——说明被卡的是开始时间，不是文件内容。

**结论：在 App 里删掉的记录，用这个通道传不回来。** 想要恢复只有两条路：找客服把服务端记录彻底删掉，或者放弃这条。

`--ignore-platform` 只能绕过客户端那层判断，服务端照样拒，别指望它能"复活"已删记录。

### 怎么确认到底传成功没有

上传接口的 `code:0` 只代表"文件收下了"，解析是异步的，失败也是静默失败。所以脚本在上传结束后会等 120 秒回查文件表：

```
解析结果：成功 50 个文件，失败 1 个
  失败：20261015_running_651266400.tcx（成功 0 / 失败 1）
```

看到失败就说明这条没进 App，常见原因就是上面那条「已被同步过」。
### 想强制重来一次

```bash
# 1. 删掉台账（或整个 tcx 目录）
rm tcx/.szxd-uploaded.json

# 2. 重新导出
python sync.py export --out tcx --types running --tz 8

# 3. 预演，确认清单
python sync.py upload --dir tcx --dry-run

# 4. 正式上传
python sync.py upload --dir tcx
```

三个连带影响先看清楚：

- **换 `--out` 目录 = 换台账**。新目录里没有台账，51 条会全部重传，服务端多半全拒
- **只改 `--tz` 重新导出、文件名不变**时，台账仍认为"传过"，平台里留的是旧时区的数据。这种必须删台账再传
- **服务端按开始时间去重**：App 里删掉的记录重传会被拒（`运动数据已被同步过`），删台账也没用
## 参数速查

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| garmin-login | `--email` / `--password` | 不传则交互输入；`--domain` 选国区/国际区 |
| login | `--phone` / `--code` | App 里获取的短信验证码 |
| export | `--out` | 输出目录，默认 `tcx` |
|  | `--types` | 活动类型，默认 `running` |
|  | `--tz` | 时区偏移，默认 8 |
|  | `--garmin-domain` | `garmin.cn`（默认）或 `garmin.com` |
| upload | `--dir` | TCX 目录 |
|  | `--osid` | 请求头 osId，默认 `1002` |
|  | `--delay` | 每个文件间隔秒数，默认 1.5 |
|  | `--dry-run` | 只预演，不上传 |
|  | `--ignore-platform` | 只跳过客户端比对，服务端仍会按开始时间拒重 |
|  | `--verify-limit` | 核对最近多少个文件的解析结果，默认 20 |
|  | `--match` | 只处理文件名含指定子串的文件 |

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
