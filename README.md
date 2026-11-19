> **公开审阅快照**：此仓库用于审阅截至 2026-11-18 的程序代码。程序源代码来自本机项目；历史说明中的实际网络地址和任务号已隐藏。本仓库不包含真实图片、数据库、日志、配置密钥、接口令牌或证书。运行数据应保存在本地，不要提交到仓库。
>
> 仓库不含任何图片文件。安装依赖后运行 `.venv/bin/python scripts/generate_review_fixture.py`，可在本机生成代码和测试所需的合成图片。以下说明中的 PDF、ZIP 也是本机生成物，可按文中命令生成。部署与测试前请先核对外置数据卷要求。
>
> 其他 Agent 请先阅读 [审核入口](README-审核入口.md)。原内部审核包脚本引用的历史截图未公开；服务运行不依赖该截图。

新环境在专用数据卷上克隆仓库后，先安装依赖并生成合成图片：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/generate_review_fixture.py
mkdir -p var
```

以下手册保留原 Mac 的运行说明；路径和已安装依赖需按新环境调整。

# 4S 告警复核试点

## 今天怎么用

1. **4S 传图。** 现场照常报警。4S 把项目编号、报警编号、类型和 1–5 张图交给这台 Mac。
2. **Mac 收好。** 图片和任务一起存在 Data 外置盘。4S 拿到任务号后就可以继续做别的事，也可以随时查进度。
3. **再判断、再回传。** 选好视觉模型后，Mac 审核图片，保存中文理由，再把结论发给 4S。是否过滤和如何展示均由 4S 决定。

用本机浏览器打开 [审核后台](http://127.0.0.1:8765/admin)，后台显示收到的报警和审核结果。用 [中文接口文档](http://127.0.0.1:8765/docs) 对接。给同事传 `docs/4S-联调包.zip` 即可，里面有接口文档、OpenAPI 和发送脚本。

## 启动和停止

终端先运行 `cd "/Volumes/Data/4s ai"`。本机依赖已安装在项目的 .venv 内，服务作为后台进程运行，可以关闭终端。

| 要做的事 | 运行 |
|---|---|
| 启动、查看是否运行 | `./4s start`、`./4s status` |
| 停止、重启 | `./4s stop`、`./4s restart` |
| 查看 4S 对接令牌 | `./4s token live` |
| 配置、验证、切换或暂停模型 | 本机后台点击「模型设置」 |
| 查看已保存的模型 | `./4s model-list` |
| 填入 4S 回调地址和令牌 | `./4s configure-callback`，然后 `./4s restart` |

在后台点击「提交图片」可按项目编号和报警编号提交图片。所有项目都使用同一套审核流程。模型关闭时，实际审核的图片先保存，下次启用会继续处理。「危险区域」和「组合报警」按一期范围直接返回无法确定并保留。

在「模型设置」中绑定平台、API 地址和 API Key，点击「读取平台模型列表」选择图片模型并保存；也可以手动填写模型 ID。API Key 保存在 Data 盘，页面不会回显；修改已保存配置时留空可继续使用原密钥。列表来自供应商，实际权限和图片输入仍须通过启用前的合成图验证。然后选择主模型、可选的备用模型和主模型超时秒数，点击「验证并启用主备」。验证失败时现有设置继续工作。暂停审核后，新任务会先保存，正在审核的任务使用原设置完成；重新启用后等待中的任务会继续处理。

模型启用时，真实安全帽、反光衣图片会发送到所选供应商并产生调用费用。「模型设置」或 `./4s model-list` 可查看当前配置。Mac 关机、休眠或外置盘断开时无法接单。关机前先停止服务，重新登录 Mac 后挂好 Data 盘再启动。本版使用手动开机启动。启动时自动恢复已经入库的任务。Mac 离线时，4S 应保留没有收到收件确认的报警，恢复连接后用原编号重传。4S 根据自身策略决定报警展示和通知。

## 把接口给 4S

外网联调运行 `./4s public-status` 查看正在使用的 HTTPS 根地址。当前使用账号绑定的固定隧道。把根地址给 4S，接口令牌通过公司安全渠道单独交给对接负责人。对接方可打开 `/docs` 或下载 `/integration.zip`，按接口文档提交报警。`GET /healthz` 成功只返回 `{"status":"ok"}`。公网网关仅开放文档、收件、查询和健康检查。真实图片需确认传输服务和模型厂商各自的数据许可。

### 接入公司的固定内网 IP

1. 先确认固定 IP 分配给这台 Mac 还是公司网关，并取得对 4S 提供的 HTTPS 地址、端口及证书信任方式。请让网络同事确认 **4S 实际发送服务器** 有到该 IP 和端口的内网或 VPN 路由。
2. 若 IP 分配给 Mac，使用现有 HTTPS 网关。证书的 IP SAN 应包含这个固定 IP。在 Data 盘的 `var/tls/` 放入 `fullchain.pem` 和 `privkey.pem`，让两端信任签发机构，然后在项目目录运行 `./4s fixed-start <固定内网IPv4>`。默认根地址为 `https://<固定内网IPv4>:8443`。Data 盘挂载后可用 `./4s fixed-start` 恢复此入口。
3. 若由公司网关提供 HTTPS，4S 使用网关分配的根地址。网关只发布接入手册内的接口及文档下载路径，转发给 Mac 的受限 HTTPS 网关。管理页继续在 Mac 本机使用。
4. 在 4S 发送服务器用公司根地址访问 `/healthz`，再从该地址下载接入包并运行 `python3 scripts/check.py --base "<公司 HTTPS 根地址>"`。确认服务正常后，在 4S 配置中替换根地址。路径、请求格式和令牌沿用现有设置。
5. 4S 通过确认的传输路径接入报警图片。若继续使用公网入口，告警图会经过第三方隧道。ngrok 免费套餐每月提供 20,000 个 HTTP 请求和 1 GB 出站流量，出站流量包含中转至 Mac 客户端的数据。首次用浏览器打开公网文档时，如出现提示，点「Visit Site」继续。[免费套餐额度](https://ngrok.com/docs/pricing-limits/free-plan-limits) · [出站流量口径](https://ngrok.com/pricing)

### 试点用 ngrok 账号固定域名

1. 首次在项目目录运行 `./4s tunnel-setup`，在终端的隐藏输入框粘贴试点账号的 Authtoken。客户端位于 `var/bin/ngrok`。首次成功连接后，屏幕显示账号分配的 HTTPS 根地址。凭据及绑定的网址分别保存在 Data 盘的 `var/ngrok.yml` 和 `var/ngrok-url.txt`。请仅通过公司的安全渠道交接凭据。[ngrok 账号凭据](https://dashboard.ngrok.com/get-started/your-authtoken)
2. 4S 配置该根地址，统一推送路径为 `/api/v1/tasks`。使用实际的项目编号和报警编号。ngrok 将请求转发给本机的对外网关，应用在 Data 盘保存图片。试点配置关闭本机 ngrok 请求查看器。账号的 Traffic Inspector 请保留默认的元数据模式，关闭 Full Capture，避免在云端保存图片正文和请求头内容。ngrok 的套餐对请求量和流量有额度限制。[账号域名](https://ngrok.com/docs/gateway/domains) · [免费套餐额度](https://ngrok.com/docs/pricing-limits/free-plan-limits)
3. Mac 重启并挂载 Data 后，运行 `./4s start` 即可启动服务并重新连接保存的固定网址。`./4s stop` 停止服务和隧道。单独执行 `./4s public-stop` 会暂停公网访问，直到执行 `./4s public-start`。
4. 迁移时先停止旧机，按迁移手册复制任务库与私有配置，将同一账号的 `ngrok.yml` 和 `ngrok-url.txt` 放到新机的状态目录，安装目标系统对应的 ngrok 到 `var/bin/ngrok`，再运行 `./4s start`。用原网址的 `/healthz` 检查服务。凭据及客户端二进制独立于源码审核包。[官方客户端](https://ngrok.com/download)

### 使用固定公网 IPv4

使用公司线路的固定公网 IPv4 可以直接部署 HTTPS。四个条件是运营商固定分配该 IP、公司网关允许入站、Mac 的内网地址有 DHCP 保留、证书的 IP 地址字段（IP SAN）与公网 IP 匹配。数据继续写在 Data 盘。

1. 网络管理员确认路由器实际 WAN 地址和固定分配记录，为 Mac 保留内网地址，在网关把公网 TCP 443 映射到 Mac 的 TCP 8443。已有 HTTPS 业务占用 443 时须先分配独立入口。公网只发布 TLS 网关，后台所在的 8765 保持内网使用。
2. 在项目的 `var/tls/` 放置服务证书链 `fullchain.pem` 和配套私钥 `privkey.pem`，仅允许运行账户读取私钥。证书须由 4S 客户端信任的 CA 签发。可使用支持 IP 地址的 ACME 客户端，按签发方式配置公网验证与自动续期。若使用 Let's Encrypt 的短期 IP 证书，证书部署成功后的钩子应运行项目中的 `4s fixed-restart` 以加载新证书。[IP 证书与 Certbot](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)
3. 在项目目录执行 `./4s fixed-start <已确认的公网IPv4>`。命令启动业务服务和独立的 HTTPS 网关，并在本机核对证书与 IP。成功后会把该地址记在 Data 盘。开机挂载 Data 后，执行 `./4s fixed-start` 复用同一地址；单独关闭固定入口用 `./4s fixed-stop`。普通业务服务重启用 `./4s restart`。
4. 从公司网络以外打开 `https://<固定公网IP>/healthz`，使用默认 TLS 验证，确认服务正常后向 4S 交付地址 `https://<固定公网IP>/api/v1/tasks`。服务离线期间 4S 保留原请求待重传。

临时隧道与固定入口可以单独启停。4S 只需将 `BASE_URL` 写入自己的环境配置。文档、OpenAPI 和联调包均可随部署直接复用。

| 操作 | 命令 |
|---|---|
| 开启 / 关闭隧道入口 | `./4s public-start` / `./4s public-stop` |
| 查看当前公网域名 | `./4s public-status` |
| 登录本机后台 | 浏览器访问 http://127.0.0.1:8765/admin，用户名 admin，密码通过 `./4s token admin` 在本机取得 |
| 回调故障修好后批量重发 | `./4s callbacks-retry live` 只重新排队已耗尽重试的回调 |

本机后台密码是独立凭据，位于数据盘 var/admin.secret。新部署可自行生成新密码。内网 8765 仍可用于已有的受信联调，HTTP 明文传输令牌，对外提供 HTTPS。公网网关在本机回环端口 8766。停止整个服务也会停止公网入口。

用 `./4s token live` 在本机取出 4S 令牌，单独交付给对接负责人。如需轮换，依次运行 `./4s stop`、`./4s rotate-token live`、`./4s start`，再用 `./4s token live` 取新令牌并通知 4S 更新。旧令牌失效，公网地址不变。

| 平台用途 | 方法和路径 |
|---|---|
| 报警图片推送 | POST /api/v1/tasks |
| 按任务号查询 | GET /api/v1/tasks/{task_id} |
| 中文手册 / PDF / 机器可读协议 | GET /docs、GET /api-guide.pdf、GET /openapi.json |

所有项目编号都按同一规则审核。以前固定返回结果的演示记录保存在数据库中，业务列表和统计不包含这些记录；实际模型审核过的记录继续显示。

## 视觉模型和回调

- 2026-11-17 的 Mac 试点已验证并保存 DeepSeek 官方直连接口 `deepseek-flash`。可切换至百炼或其他支持图片、Chat Completions 与 JSON 输出的模型。每个候选单独保存地址、型号和密钥，后台切换后立即生效，正在处理的任务使用原模型完成。更换步骤和回退方法见 [换模型与迁移](docs/review/02-换模型与迁移.md)。
- 启用备用模型后，主模型超时或发生接口、格式等技术故障时，本次任务自动尝试备用模型；下一张图片仍先使用主模型。`uncertain` 是正常业务结论，不会触发备用模型。主备均失败时按原重试规则处理，最终技术失败的结论为空。详情页记录每次主备调用和结果；超时后供应商可能仍收取主模型费用。
- 正式推理会将图片或裁好的区域发送给选中的供应商。用户可在内部详情查看已保存的型号、规则版本、耗时和理由。图片、结果和任务持久化在选定的数据卷。PPE 准确度、真实延迟、价格和公司数据处理要求需要由试点方评估。[DeepSeek 图片接口](https://api-docs.deepseek.com/zh-cn/guides/vision/) · [百炼视觉接口](https://help.aliyun.com/zh/model-studio/vision/)
- 4S 需给出接收回调的 HTTPS 地址和单独的 Bearer 令牌，并确认 2xx 为已保存。回调失败有自动重试，必要时从详情页重发。平台可同时使用查询接口。手工重发会开启新的回调尝试批次，字段 callback_attempts 显示该批次次数。
- 一期审核安全帽和反光衣。一条报警一次提交 1–5 张图，服务逐张复核并回传一个报警级总结果。项目内报警 ID 是否唯一、已有类型码映射、是否传整图或矩形 ROI、组合类型含义、危险区域定义和故障时的展示策略，需要和 4S 确认。试点并发、识别表现和客户通知逻辑尚需两侧实测。

## Data 盘上保存什么

| 路径（相对本项目） | 内容 |
|---|---|
| var/live/tasks.sqlite3 | 正式任务、原图二进制、审核理由、重试及回调状态 |
| var/mock/tasks.sqlite3 | 旧版历史文件，当前服务不使用 |
| var/config.json | 本机令牌、公司模型和 4S 回调配置，限本机账户读取 |
| var/service.log | 服务运行和错误日志 |
| var/tmp、var/pip-cache、.venv | 服务临时目录、依赖缓存、运行环境 |

本 Mac 默认从 /Volumes/Data 下启动。迁移时 FOUR_S_VOLUME 可指定专用挂载卷，项目和状态须位于该卷内。macOS 启动时另查物理卷为外置；Linux 需在目标机验收。SQLite 以事务一起提交原图和任务，WAL 日志留在同一目录。现阶段保留所有已接收的记录，备份时可先停止服务再复制 var/live 和 var/mock 两个完整目录。试点不自动清理历史图。正式数据和配置请留在外置盘，不要打进发给同事的联调包。

## 如何复验

项目目录中运行 `.venv/bin/python -B tests/pilot.py` 和 `.venv/bin/python -B tests/model_fallback.py`。它们使用 Data 盘上的一次性隔离库、本机 HTTP 模型替身及回调接收器，验证并发去重、模型与回调重试、中断恢复、主备切换及手工重发。测试会快进隔离库中的重试等待时间。后台支持上传、筛选、查看中文理由和放大原图。外部平台的网络连通、正式回调以及现场识别质量还需双方验收。

若重建本机环境，在同一外置盘目录执行：

```sh
mkdir -p var/tmp
TMPDIR="$PWD/var/tmp" python3 -m venv .venv
TMPDIR="$PWD/var/tmp" PIP_CACHE_DIR="$PWD/var/pip-cache" .venv/bin/python -m pip install -r requirements.txt
./4s start
```

更新 `docs/4S-API.md` 后，用装有 ReportLab 的 Python 运行 `scripts/render_api_pdf.py`，再运行 `.venv/bin/python -B scripts/package.py` 重建接入包。PDF 默认使用 Mac 的中文字体，其他系统可用 `FOUR_S_PDF_FONT` 和 `FOUR_S_PDF_BOLD` 指定中文 TrueType 字体文件。

Windows Server 2016 安装及切换步骤见 [Windows 部署](windows/部署.md)。
