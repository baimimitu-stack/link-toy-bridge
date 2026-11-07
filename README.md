# LINK：把 Intiface 设备接给手机里的角色和 Grok

一个人用的远程控制链路：**手机里的角色（或 Grok）调一个 MCP 工具，你电脑上的 Intiface 设备就动**。
MCP 放在服务器上，电脑只留一个很薄的中继；手机只填一串地址。

> 这份文档按"照着做就能复现"写。每一步都有命令和验证方法；踩过的坑单独成章；
> 最后有一段可以直接贴给 AI 助手（比如 Claude Code）让它代你施工的指令。

---

## 目录

1. [它是什么、为什么这样搭](#1-它是什么为什么这样搭)
2. [你需要准备什么](#2-你需要准备什么)
3. [十个工具](#3-十个工具)
4. [搭建步骤（照着做）](#4-搭建步骤照着做)
5. [手机和 Grok 怎么填](#5-手机和-grok-怎么填)
6. [验证清单](#6-验证清单)
7. [日常使用](#7-日常使用)
8. [掌机面板怎么看](#8-掌机面板怎么看)
9. [文件和配置项](#9-文件和配置项)
10. [安全须知](#10-安全须知)
11. [常见问题和踩过的坑](#11-常见问题和踩过的坑)
12. [一套可复用的做事流程](#12-一套可复用的做事流程)
13. [给 AI 助手的施工指令](#13-给-ai-助手的施工指令)

---

## 1. 它是什么、为什么这样搭

```
手机里的角色 / Grok
   │  一串地址（钥匙写在地址里，或者地址 + 钥匙两个框）
   ▼
服务器上的 MCP（linkmcp.py，十个工具）        ← 一直开着，开机自启
   │  WebSocket（电脑主动连出来，电脑不用开任何入站端口）
   ▼
电脑上的中继（linkrelay.py，只搬运）
   ▼
本机桥（bridge.py，Buttplug ⇄ MCP，限幅、自动归零）
   ▼
Intiface Central ──蓝牙──▶ 设备

另外：本机掌机面板（mediator.py + web/）= 一块有屏幕的小机器，
      看得见谁在什么时候让设备动了；只给你自己看，不说话、不思考。
```

几条设计原则（也是后面所有决定的依据）：

| 原则 | 做法 |
|---|---|
| **只有碰蓝牙的那一小段留在电脑** | 蓝牙和 Intiface 只能在电脑上；其余全放服务器，所以手机不用管你电脑的 IP、防火墙、同不同一个 WiFi |
| **服务器一直在，电脑可以不在** | 电脑关机时角色照样能"连一下"、列出工具；真调用得到一句「掌机没在线」，不是连接失败 |
| **一个人用，别设计成防别人** | 唯一的门是地址里那串 48 位随机字符（和 xiuxiuda 一个路数）。不做账号体系、不做多人 |
| **工具名用中性代号** | MCP 的工具名和说明会进模型上下文。叫 `output_a` 比叫具体用途干净；对照表只写在本文第 3 节 |
| **逻辑只写一份** | 限幅、自动归零、错误翻译都在本机桥里；服务器和中继只搬运，不重复实现 |
| **电脑端一键开、一键关** | 桌面两个图标；后台全部静默；关掉掌机窗口只缩到托盘，不会误停 |

适用：Windows 电脑（启动脚本是 PowerShell）、一台能装 Python 的 Linux 服务器（可选有域名）。
桥、中继、中介本身是纯 Python，别的系统也能跑，只是一键启动脚本要自己改。

---

## 2. 你需要准备什么

| 东西 | 说明 |
|---|---|
| 一台电脑（Windows） | 装 **Python 3.12+**、**Intiface Central**（它负责蓝牙和设备） |
| 一台 Linux 服务器 | 有公网 IP；能 `ssh`、有 `sudo`、装得了 `python3-venv`。我们在 Ubuntu 24.04（Python 3.12）和 26.04（Python 3.14）上跑过 |
| 域名（可选） | 有域名 + Caddy/Nginx → 能走 **https**，手机网页版也能用。没域名 → 只能直连 `http://IP:端口`，只有装成 App 的手机端能用（浏览器会拦 https 页面调 http） |
| 云厂商防火墙 | 没域名直连端口时，要在**云控制台**的防火墙/安全组放行那个 TCP 端口（光服务器本机放行没用） |
| 一个会调 MCP 的客户端 | 本项目配套的是"小手机"外接功能；也可以是 Grok 的自定义连接器、Cursor 等任何能填 MCP 地址的 |

---

## 3. 十个工具

服务器端（`linkmcp.py`）和本机桥（`bridge.py`）的工具名、参数、说明**完全一致**。

| 对外名 | 参数 | 实际是什么 |
|---|---|---|
| `link_status` | — | 链路状态：桥连没连上 Intiface、几个设备、上次出错原因 |
| `channel_list` | — | 列出已连设备：编号、名称、支持哪几类输出 |
| `channel_scan` | `seconds`=8 | 重新扫描设备 |
| `output_a` | `val` 0-100，`sec`，`device` | 震动（Vibrate） |
| `output_b` | 同上 | 摆动（Oscillate） |
| `output_c` | 同上 | 旋转（Rotate） |
| `output_pos` | `val`，`sec`，`device` | 行程位置（Position） |
| `output_seq` | `seq`=[20,60,20]，`step`=1，`device` | 按序列播放一串强度，放完自动归零 |
| `output_stop` | `device` | 立刻归零（不填就是全部） |
| `channel_level` | `device` | 查电量 |

- `val` 一律 0-100，0 是归零；`sec` 到点自动归零，不写就一直保持。
- `val` 可以写小数或字符串（`"40"`），桥会容错；`MAX_STRENGTH` 能给上限加一道保险。
- 想改工具：**两边一起改**（`bridge.py` 和 `linkmcp.py`），否则列出来的和真能执行的不一致。

---

## 4. 搭建步骤（照着做）

### 4.0 先决定：有没有域名

- **有域名**（推荐）：走 Caddy 反代，得到 `https://你的域名/link/…`
- **没域名**：服务直接监听 `0.0.0.0:端口`，得到 `http://服务器IP:端口/…`（要在云控制台放行端口）

### 4.1 拿到代码，装依赖（电脑）

```powershell
git clone <你的仓库地址> link-toy-bridge
cd link-toy-bridge
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

装好后把 `.venv\Scripts\python.exe` 的完整路径记下来，等下写进 `.env` 的 `LINK_PY`。
（不写也行，启动脚本会去 PATH 里找 `python.exe`，但那个多半没装依赖。**建议写。**）

### 4.2 生成密钥

```powershell
python tools/gen_secrets.py
```

它会打印三类密钥：电脑用的 `BRIDGE_TOKEN`、电脑和服务器共用的 `RELAY_TOKEN`（电脑那边叫 `LINK_RELAY_TOKEN`）、服务器用的 `LINK_TOKEN`。
**`RELAY_TOKEN` 和 `LINK_TOKEN` 必须不一样**，程序会拒绝启动。别把它们发到聊天里、别提交到 git。

### 4.3 服务器部署

**① 写服务器的环境文件**（权限要收紧）：

```bash
sudo tee /etc/link-mcp.env >/dev/null <<'EOF'
LINK_TOKEN=（gen_secrets 的 LINK_TOKEN）
RELAY_TOKEN=（gen_secrets 的 RELAY_TOKEN）
LINK_HOST=127.0.0.1      # 前面有 Caddy 时；没有反代、直接对外就写 0.0.0.0
LINK_PORT=8802           # 直接对外的话常用 8801，记得云防火墙放行
EOF
sudo chmod 640 /etc/link-mcp.env
```

> ⚠️ 不要用 Windows PowerShell 的管道（`"..." | ssh ...`）去写这个文件：它会在文件开头加一个看不见的 BOM，第一个变量名被污染，服务会报"必须设置 LINK_TOKEN"。用 `printf`/`tee` 在服务器上写，或用 Git Bash。

**② 传文件并安装**（在电脑上，仓库根目录）：

```bash
scp linkmcp.py requirements-server.txt deploy/link-mcp.service 你@服务器:/tmp/
ssh 你@服务器 'bash -s' < deploy/deploy-server.sh
```

脚本会：建一个专用低权限用户 `linkmcp`、装 venv 和依赖、装 systemd 服务并开机自启。成功时最后会打印 `active` 和 `{"ok":true,"pc_online":false,...}`。

**③a 有域名：加一段反代**（先备份！）：

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%Y%m%d%H%M)
# 把 deploy/Caddyfile.snippet 的内容加进你站点块里，然后：
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl https://你的域名/link/health        # 应该看到 {"ok":true,"pc_online":false,...}
```

**③b 没域名：直连端口**

把 `/etc/link-mcp.env` 里改成 `LINK_HOST=0.0.0.0`、`LINK_PORT=8801`，重启服务；然后在**云控制台**防火墙加入站规则：TCP 8801，来源全部 IPv4。
验证（在**另一台机器**上，别用你电脑——代理软件可能吃掉连接）：`curl http://服务器IP:8801/health`。

### 4.4 电脑端配置

```powershell
copy .env.example .env      # 然后编辑 .env
```

必改的几项：

```ini
BRIDGE_HOST=127.0.0.1
BRIDGE_TOKEN=（gen_secrets 的 BRIDGE_TOKEN）
LINK_SERVERS=wss://你的域名/link              # 没域名：ws://服务器IP:8801 ；多台逗号隔开
LINK_RELAY_TOKEN=（和服务器 RELAY_TOKEN 同一串）
LINK_PY=C:\...\link-toy-bridge\.venv\Scripts\python.exe
```

### 4.5 启动

1. 打开 **Intiface Central**，点 **Start Server**；到 Devices 页点 **Start Scanning**，把设备开机，等它出现。（这一步别人替不了，蓝牙和设备都在它手里）
2. 双击仓库里的 **`Start LINK.bat`**。后台静默起：桥 → 中介 → 中继 → 保活，然后弹出掌机窗口。
3. 掌机顶上四盏灯（桥 / Intiface / 玩具 / 服务器）都亮、写着「一切就绪 ✓」就好了。

---

## 5. 手机和 Grok 怎么填

**钥匙就是服务器的 `LINK_TOKEN`。** 两种写法服务器都认：

| 客户端 | 网址 | 钥匙 |
|---|---|---|
| **小手机**（有「网址 + 钥匙」两个框） | `https://你的域名/link/mcp` | `LINK_TOKEN` |
| **Grok 自定义连接器**（只有一个网址框） | `https://你的域名/link/k/<LINK_TOKEN>/mcp` | 留空 |
| 没域名的备用 | `http://服务器IP:8801/mcp`（或 `/k/<LINK_TOKEN>/mcp`） | 同上 |

- 小手机「经站里转发」选**关**。
- 钥匙错或没带 → 返回 **404**（不是 401），当作这个地址不存在。

---

## 6. 验证清单

按顺序，哪一条不对就停在那一条查。

```bash
# ① 服务器活着（pc_online 在电脑没开时是 false，正常）
curl https://你的域名/link/health

# ② 钥匙对，能列出 10 个工具（Bearer 写法）
curl -s -X POST https://你的域名/link/mcp \
  -H "Authorization: Bearer <LINK_TOKEN>" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# ③ 钥匙错应该是 404
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://你的域名/link/mcp \
  -H "Authorization: Bearer wrong" -H "Content-Type: application/json" -d '{}'
```

电脑 `Start LINK.bat` 之后：

- 再 `curl .../link/health` → `"pc_online":true`
- 调 `link_status` → 返回 JSON（`upstream_connected` 应为 true）
- Intiface 里扫到设备后调 `channel_list` → 能看到设备
- 调 `output_a`（`val`=10，`sec`=2）→ 设备轻轻动 2 秒后停
- 调 `output_stop` → 立刻停

**急停有三层，记住最快的：** ① Intiface 里把滑块拖到 0；② 设备本体关机；③ 双击 `Stop LINK.bat`。

---

## 7. 日常使用

| 想做什么 | 怎么做 |
|---|---|
| 开 | 开 Intiface（Start Server + Scan）→ 双击 `Start LINK.bat` |
| 看状态 | 看掌机顶上四盏灯和那句诊断 |
| 暂时不看 | 点掌机窗口的关闭——**只是缩到托盘**，后台一切照旧，服务器还能看到电脑 |
| 把窗口叫回来 | 点托盘图标；或再双击 `Start LINK.bat`（不会多开） |
| 全关 | 托盘右键「全部关闭并退出」；或双击 `Stop LINK.bat` |

电脑睡眠或关机后链路会断：保活脚本会让 Windows 在空闲时不睡眠、屏幕常亮（不改你的电源设置），但**合盖或手动点睡眠仍然会睡**。
玩具如果自己断了蓝牙，要在 Intiface 里重新 Scan，桥和中继会自己重连。

---

## 8. 掌机面板怎么看

窗口顶上四盏灯，下面一句话告诉你哪里没通、该干嘛：

| 灯 | 意思 | 不亮时那句话 |
|---|---|---|
| 桥 | 本机桥在不在 | 桥没开：双击 Start LINK |
| Intiface | 桥连上 Intiface 没有 | Intiface 没连上：打开它，点 Start Server |
| 玩具 | 找到设备没有（灯上显示设备名） | 玩具没找到：Intiface 里点 Start Scanning |
| 服务器 | 中继连着几台服务器 | 服务器没连上：手机现在调不到 |

下面是四个通道的值条和事件流（红边 = 角色调的，绿边 = 你在面板上按的），再往下是手动按键：选通道、调值和秒、发送、归零、扫通道。
「更多」里有保持唤醒、刷新。有两套外观，右上角「外观」切换。

---

## 9. 文件和配置项

| 文件 | 跑在哪 | 作用 |
|---|---|---|
| `bridge.py` | 电脑 | **本机桥**：Buttplug ⇄ MCP。限幅、自动归零、把错误翻成人话、每次被调用都报给中介 |
| `mediator.py` + `web/` | 电脑 | **掌机面板**：本机窗口、事件流、状态灯、手动按键 |
| `linkrelay.py` | 电脑 | **中继**：连服务器，把调用转给本机桥，每 10 秒向面板报平安 |
| `linkmcp.py` | 服务器 | **服务器端 MCP**：十个工具 + 中继入口 + 钥匙校验 |
| `link-launcher.ps1` | 电脑 | 一键启动/关闭；常驻托盘。**只能写 ASCII**（中文用 `\uXXXX` 转义） |
| `Start LINK.bat` / `Stop LINK.bat` | 电脑 | 上面脚本的两个入口 |
| `keepawake.ps1` | 电脑 | 防睡眠、屏幕常亮，不改系统设置 |
| `check.py` | 电脑 | 自检：按小手机的方式握手一次，列出工具 |
| `tools/gen_secrets.py` | 电脑 | 生成密钥 |
| `deploy/` | 服务器 | systemd 单元、环境文件样例、Caddy 片段、安装脚本 |

**电脑 `.env`**

| 变量 | 默认 | 说明 |
|---|---|---|
| `INTIFACE_URL` | `ws://127.0.0.1:12345` | Intiface 的地址 |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `127.0.0.1` / `8791` | 本机桥监听。默认只听本机，它只给本机中继调 |
| `BRIDGE_TOKEN` | — | 本机桥的钥匙；本机（127.0.0.1）来的请求不查，从别处来的必须带 |
| `MAX_STRENGTH` | `100` | 输出上限 |
| `MEDIATOR_PORT` / `MEDIATOR_TOKEN` | `8800` / 空 | 面板；不设钥匙就是本机免鉴权 |
| `MEDIATOR_HOST` | `127.0.0.1` | 面板监听地址；默认只有本机能开，改成 `0.0.0.0` 前务必先设 `MEDIATOR_TOKEN` |
| `LINK_SERVERS` | — | 服务器中继地址，可多个，逗号隔开 |
| `LINK_RELAY_TOKEN` | — | 进服务器的暗号 |
| `LINK_PY` / `LINK_INTIFACE` | 自动找 | 启动脚本找不到时才写 |

**服务器 `/etc/link-mcp.env`**

| 变量 | 默认 | 说明 |
|---|---|---|
| `LINK_TOKEN` | — | 手机/Grok 的钥匙（≥24 位） |
| `RELAY_TOKEN` | — | 电脑中继的暗号（不能和上面相同） |
| `LINK_HOST` / `LINK_PORT` | `127.0.0.1` / `8802` | 监听 |
| `LINK_CALL_TIMEOUT` | `75` | 等电脑回话的秒数 |

---

## 10. 安全须知

这是一条**能远程驱动你身体上设备**的链路，请按最保守的来：

1. **密钥只有你知道。** `LINK_TOKEN`、`BRIDGE_TOKEN`、`RELAY_TOKEN` 都不要发聊天、不要截图、不要提交 git（`.gitignore` 已挡 `.env`）。怀疑泄露就重新生成、改完重启服务和电脑端。
2. **地址就是钥匙。** 带钥匙的长网址等于密码，只填进你自己信任的客户端。
3. **没域名走 http 是明文**，钥匙在网络上是裸的。能用 https 就用 https。
4. **留一个最快的急停**（见第 6 节），并确认你随时够得着。
5. 给角色用的时候，`MAX_STRENGTH` 调到你能接受的上限；先从 `val`=10、`sec`=2 开始试。
6. 小手机里的「一小时内默认同意」会让角色这段时间调工具不再问你，不在场也一样；用之前想清楚。
7. 本仓库**已去掉**面板里原来会把带钥匙地址发给第三方二维码网站的功能。如果你自己加回来，别发密钥。
8. 服务端用专用低权限用户、`NoNewPrivileges`、`ProtectSystem=strict` 跑；不要改成 root。

---

## 11. 常见问题和踩过的坑

| 现象 | 原因 / 怎么办 |
|---|---|
| 服务器 `systemctl` 起不来，日志说「必须设置 LINK_TOKEN 和 RELAY_TOKEN」 | 环境文件第一行带了 **BOM**（Windows PowerShell 管道写的）。用服务器上的 `printf`/`tee` 重写 |
| 两串密钥一样程序拒绝启动 | 设计如此：`LINK_TOKEN` 和 `RELAY_TOKEN` 必须不同 |
| `curl` 服务器没响应 | 没域名直连时多半是**云控制台防火墙没放行端口**。在另一台机器上测，不要在开着 Clash 等代理的电脑上测——它会吃掉连接，表现为"能连上但没响应/502" |
| 掌机说「服务器没连上」，但服务器正常 | 电脑中继没起，或 `LINK_RELAY_TOKEN` 和服务器 `RELAY_TOKEN` 不一致；看 `logs/linkrelay.out.log` |
| 调工具得到「掌机没在线」 | 服务器看不到电脑：电脑关机/睡眠/没点 Start LINK/中继断了 |
| 调工具得到「电脑上的桥没开」 | 中继在、桥不在：双击 Start LINK（它会补起桥） |
| 调工具得到「上游没连上 … WinError 1225」 | **Intiface Central 没开**，或没点 Start Server |
| 调工具得到「现在一个通道都没有」 | 设备没连：Intiface 里 Start Scanning；设备会自己休眠/断开，用之前先扫一下 |
| 关了掌机窗口后手机调不到了 | 老版本关窗口会全停；现在只缩到托盘。如果是全停，说明用的是旧脚本 |
| 手机提示 404 | 钥匙不对或没带（故意返回 404 不暴露这个地址存在） |
| 网页版手机提示被拦 | 用了 `http://` 地址：浏览器禁止 https 页面调 http。换 https 地址，或用装成 App 的客户端 |
| 角色发了指令却没执行 | 小手机侧要放宽解析（括号、引号、参数别名），见"小手机怎么认指令"那类说明；桥的 `calls.log` 能看到真收到了什么 |
| `linkmcp.py` 报 `transport security`/Host 相关错 | 服务器端已关掉本机 Host 限制（`enable_dns_rebinding_protection=False`）；如果你自己改回去，对外访问会被拒 |
| 用的 MCP SDK 版本不对 | 本项目用 `mcp==2.3.0` 的新接口（`mcp.server.mcpserver.MCPServer`），旧版 1.x 不兼容。装 `requirements.txt` 里的版本 |
| PowerShell 里拼远程命令总出错 | 别在 PowerShell 里拼带引号的 ssh 命令：把脚本写成文件，`ssh 主机 'bash -s' < 脚本` |
| 修改 Caddy 后别的站点挂了 | 改之前**一定备份**，`caddy validate` 过了再 `reload`；出问题就把备份拷回去再 reload |

---

## 12. 一套可复用的做事流程

做这套东西（以及以后任何类似的"改动 → 上线"）用的是同一个节奏，值得原样照搬：

1. **先讲方案，再动手。** 动代码之前先说清：要改哪些文件、运行逻辑是什么、有哪些我拿主意的点。等一句"可以"。
2. **改动尽量小，一块一块来。** 别一次生成一大堆；每块改完就能单独验证。
3. **改在本机，先测。** 单元测试 + 类型检查 + 端到端（能起一份临时服务就起一份，端口用 18xxx、密钥用临时的，测完关掉）。
4. **能看到的界面要亲眼看一眼。** 用预览开手机宽度截图，别只看"类型检查过了"。
5. **列清单再上线。** 清单写四样：会发出去什么、确认没带上什么、怎么回滚、影响谁。**等明确的"可以"，不要把"我觉得你会同意"当成同意。**
6. **上线前备份，上线后验证。** 改配置先备份；上线后从**公网**测一遍，不要只看本机。
7. **留回滚。** 旧版本留在旁边一份（例如 `pwa.old`、`Caddyfile.bak-…`）。
8. **写发布记录，再提交。** 记录里写：改了什么、没改什么、没亲眼验证的地方、入口哈希/版本、上一版在哪。
9. **密钥永远不进聊天、不进 git。** 要给用户看密钥时，写进他们本机的文件，让他们自己去拿。
10. **谁的东西谁点头。** 蓝牙、设备、Intiface 的开关只有用户能点；对外发布（GitHub、服务器配置）先问。

---

## 13. 给 AI 助手的施工指令

把下面整段贴给 Claude Code（或别的能操作终端的 AI），再把你的服务器和电脑信息补进去。它会按本文第 4–6 节施工。

```text
任务：照着仓库根目录的 README.md 搭建 LINK（第 4 节），并按第 6 节逐条验证。

背景信息（我来填）：
- 电脑：Windows，仓库已 clone 在：<路径>
- 服务器：<ssh 别名或 user@ip>，系统：<Ubuntu 版本>，有没有域名：<域名 / 没有>
- 反代：<Caddy / Nginx / 没有>；没域名时我会自己去云控制台放行端口

规矩（务必遵守）：
1. 动手前先把你要做的步骤列给我，等我说"可以"。
2. 密钥用 tools/gen_secrets.py 生成；不要把任何密钥打印到聊天里，要给我看就写进我本机的文件并告诉我路径。
3. 服务器环境文件用 printf/tee 在服务器上写，不要用 Windows PowerShell 的管道（会带 BOM）。
4. 远程命令写成脚本文件再 `ssh host 'bash -s' < 脚本`，不要在 PowerShell 里拼带引号的命令。
5. 改 Caddy/Nginx 前先备份，validate 通过再 reload；不要动我服务器上已有的其他站点和服务。
6. 验证要从公网测（第 6 节），没域名时在另一台机器上测端口。
7. Intiface Central 的 Start Server / Start Scanning 和设备开机只有我能做，轮到这一步时停下来告诉我。
8. 每做完一大步汇报一次：做了什么、验证结果、还差什么。最后给我一份"手机/Grok 要填什么"（写进本机文件，不贴密钥）。
```

---

## 许可与说明

- **许可：个人使用，禁止再分发**（见根目录 [LICENSE](LICENSE)）。你可以自己用、自己改；
  不可以把它（原版或修改版）再发给别人、传到任何地方、商用或冒用。这**不是**开源许可证（不是 MIT）。
  GitHub 的 fork 只用于查看和提交修改建议。想做许可里禁止的事，先到 Issues 联系作者。
- **只能用在知情、同意的成年人身上**，并且要保证对方随时能停下（第 10 节）。
- 依赖：[Intiface Central / Buttplug](https://intiface.com/)、`mcp` Python SDK、Starlette、Uvicorn、websockets，各自有各自的许可。
- 这是个人自用工具的整理版，不提供任何担保。使用风险自负，尤其是第 10 节。
