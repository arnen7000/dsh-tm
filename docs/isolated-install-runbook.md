# 隔离安装 Runbook（多版本并存 + 踩坑全集）

> 适用：Windows 10/11。目标 —— 任意多个 dsh 版本并行、互不影响；卸载其中一个绝不腐蚀另一个。
>
> 本文讲的是**手工做隔离安装**的全流程与踩坑。用管理器（`dsh_manager.pyw`）的话这些它都替你做了
> —— 但知道底下发生了什么，排障时差别很大。
>
> 文中 `dsh-012rc1` / `dsh-012rc1-home` 只是**示例目录名**；管理器生成的槽叫 `dshV1` / `dshV2` …，
> 隔离机制完全一样。端口也同理，示例里的 3080/4080 换成你自己的槽端口即可。

---

## 0. 五条隔离轴（违反任一条都会冲突 / 腐蚀）

| 轴 | 做法 |
|---|---|
| ① 进程隔离 | 每版用**各自独立的 node 运行时**启动，内存互不干扰 |
| ② 数据隔离 | 每版独立 `DSH_HOME`（自己的 `.dsh-home`） |
| ③ 端口隔离 | 每版独立端口，不能两个都听同一个 |
| ④ 运行时隔离 | 每槽自带一份 node，装依赖与运行期用同一份（见 §6 的 ABI） |
| ⑤ 临时目录隔离 | 每槽独立 `temp_dir`，避免 TEMP 互相覆盖 |

> 经验：**dsh 版本 ≠ node 版本**。node 只是运行时，真正锁 node 版本的是原生模块 ABI（见 §6）。
> "每版自带 node"是**刻意隔离**的选择，不是铁律 —— 只要 ABI 兼容，两版共用同一份 node 也能跑。

---

## 1. 安装（隔离版走独立目录，绝不用 npm -g）

```bash
mkdir <安装根>\dsh-012rc1 && cd <安装根>\dsh-012rc1
npm init -y
npm i @deepseek-ai/dsh@0.1.2-rc.1          # 落点 node_modules/@deepseek-ai/dsh
mkdir <安装根>\dsh-012rc1-home               # 隔离 DSH_HOME
```
全局安装则是另一回事：`npm install -g @deepseek-ai/dsh@<版本号>`，数据默认在 `~/.dsh`。两种装法可以并存，但**别让它们用同一个端口或同一个数据目录**。

### 让隔离槽自带 node 运行时（脱离其它软件）
dsh 的 `node-pty` 是原生模块，按**装依赖时那个 node 的 ABI** 编译。所以槽里必须放一份自包含的 node（仅 `node.exe` 即可，约 90 MB，无外部 DLL），装依赖和运行期都用它：
- 来源：机器上任意满足版本红线的 node（见 §6），或管理器包内的 `runtime\node`。
- 好处：**卸载 / 升级其它软件、或系统 node 变化，都不会搞垮这个槽**。
- ⚠️ 换 node 版本会破坏 ABI（`node-pty` 崩）—— 要换就连 `node_modules` 一起重装。

---

## 2. 启动（隔离 home + 隔离端口）

```bash
DSH_HOME=<安装根>\dsh-012rc1-home node "<安装根>\dsh-012rc1\node_modules\@deepseek-ai\dsh\lib\bin.js" --profile web --no-open
```
- 浏览器开 `http://127.0.0.1:<该槽端口>`。另一个槽用自己的端口，可同时在线。
- **有些版本的端口只能来自 profile 补丁，而不是 `--port`**：早期 dsh（如 `0.1.2-rc.1`）的 `web` 启动器没把 `--port` 正确转给 web-app，直接传会报 `unknown option '--port'`。改法：把端口钉在 `$DSH_HOME/profiles/web/cordis.patch.yml` 的 `webserver` 行（按 bundle→patch 顺序最后覆盖）。模板见 `config/cordis.patch.yml.template`。**新版本没有这个限制**，管理器直接用 `--port` 传。

> 配置叠加顺序：bundle 清单 → profile `cordis.patch.yml` → home 级 `cordis.patch.yml` → `--patch`。最后写入的层获胜，所以钉在 profile patch 一定生效。

---

## 3. 装插件隔离（只进隔离 home 的 web profile）

```bash
DSH_HOME=<安装根>\dsh-012rc1-home node "<安装根>\dsh-012rc1\node_modules\@deepseek-ai\dsh\lib\bin.js" plugin --profile web add <pkg>
```
- 插件装进 `$DSH_HOME/profiles/<profile>`，不影响别的槽。
- **铁律**：任何「装 / 卸 / 跑 / 卸插件」操作都必须**点名版本 + 带 `DSH_HOME` + 用绝对路径**。绝不对隔离槽执行裸 `dsh ...`（裸 `dsh` 会落到 PATH 上那个全局安装的数据目录，把两个环境搅在一起）。

---

## 4. 坑：profile / 凭据死锁（atomic-write 超时）

**坑一：`<DSH_HOME>\profiles\node_modules.lock`**
若指向已死 PID（如被杀的 `npx` 残留），会卡住该槽的 `web` 启动，报 `atomic-write: timed out waiting for the writer lock`。
处理（确认没有该槽的 dsh 进程在跑后改名，可逆）：
```bat
rename "<DSH_HOME>\profiles\node_modules.lock" "node_modules.lock.bak"
```

**坑二：`<DSH_HOME>\.credentials.yaml.lock`**
dsh 启动时写浏览器会话密钥 `.credentials.yaml` 的瞬时锁，里面是持有者 PID。若在启动窗口期内被强杀 / 崩溃，锁会残留 → 下次启动同样报 `atomic-write: timed out waiting for the writer lock`，整进程崩。
处理（确认没有该槽进程后删锁即可，`.credentials.yaml` 本身**保留**）：
```bat
del "<DSH_HOME>\.credentials.yaml.lock"
```
管理器会在「启动」前自动清理「PID 已死」的该锁，日常点按钮不会复发。

---

## 5. 坑：端口残留（EADDRINUSE）

之前没杀就关掉窗口，`node.exe` 会继续占着那个端口。下次再起必报 `EADDRINUSE`。
处理：管理器「杀全部」按**各槽 `bin.js` 签名**清理 dsh（含上次遗留、**不是**本管理器启动的孤儿），非 dsh 的 node 一律不碰；或 `netstat -ano | findstr :<端口>` 找 PID 后 `taskkill /PID <pid> /T /F`。

---

## 6. 坑：node 版本 / node-pty ABI

- **Node 红线 ≥ 22.19**：低于它缺 `node:zlib.createZstdDecompress` 和 `AbortSignal.timeout`，直接报错。新版 dsh（0.2.2+）要求更高，见其 `engines.node`。
- **`node-pty` ABI**：dsh 的终端功能依赖 `node-pty` 原生模块，按装依赖时那个 node 的 ABI 编译。**换了 node 就必须重装 `node_modules`**（或 `pnpm rebuild node-pty`）。纯 web 功能基本是 JS，但终端会用到它。
- **两个 dsh 能共用一个 node 吗？** 能 —— 只要该 node 的 ABI 兼容它们各自加载的原生模块。让每槽自带 node 是为解耦，不是强制。

---

## 7. 坑：Shell 语法（cmd.exe 不认 bash 内联 env）

在 `cmd.exe` 里跑隔离版**不能**写 `DSH_HOME=... node ...`（报 `'DSH_HOME' is not recognized`）。正确写法：
1. PowerShell：`$env:DSH_HOME='...'; node "..." --profile web --no-open`
2. Git Bash：`DSH_HOME=... node "..." --profile web --no-open`（OK）
3. cmd 的 `set`：`set DSH_HOME=...` 回车，再 `node "..." ...`；或单行 `set DSH_HOME=... && node ...`

管理器用 `subprocess.Popen(env={...})` 直接传环境变量，**不经过 shell**，双击 .lnk 无此问题。

---

## 8. 坑：另一会话沉淀（来自《使用手册》）

- **`'pnpm' is not recognized`**：`dsh plugin` 把参数转发给 pnpm，而机器 PATH 里没有 pnpm。先 `npm install -g pnpm`，或直接进 profile 目录执行 `pnpm install`。
- **`github:` 格式装插件只加依赖、不写挂载行**：必须手动补 `cordis.patch.yml` 的 `- insert:` 段，否则插件不加载。
- **PowerShell 里 `dsh` 报脚本禁用**：执行策略限制 `dsh.ps1`；改用 `dsh.cmd`（cmd shim）或放行策略。
- **浏览器打不开**：端口被占 → `netstat -ano | findstr :<端口>` → kill PID。
- **插件装完不加载**：查 `cordis.patch.yml` 的 id/name 拼写。

---

## 9. 浏览器信任 token（**版本**属性：0.1.5+ 有，0.1.0-rc.8 没有）

新版 web 启动打印 `dsh web: http://127.0.0.1:<port>/?token=<每 boot 新生成>`。**不带 token 打开裸 URL 会 401**（`dsh web authentication required; reopen the URL printed by dsh web`）。`0.1.0-rc.8` 打印的是**无 token** 的 `http://127.0.0.1:3080`，直接开即可。

⚠️ **别把这条当成"隔离版特有"**：它取决于 **dsh 版本**，与 `$DSH_HOME` 是否隔离无关。源码依据：`0.1.0-rc.8` 的 `dsh-web-app` 里 `localWebUrl()` 返回裸 loopback 地址，其 `dsh-client-connection` 自述 "explicitly not authentication"；`0.1.5` 起 `dsh-client-connection` 引入 `TOKEN_QUERY = "token"` 与 `launchToken`，`dsh-web-app` 打印的变量名直接叫 `authenticatedUrl`。结论：**把任意一个槽换到 0.1.5+，那个槽就需要 token 了**。

管理器不猜版本、按槽处理：启动时布防扫描该槽端口的入口行，抓到完整 URL 就存内存；「打开 Edge / Chrome」用带 token 地址；要粘到别的浏览器 / 别的机器就用「**复制入口 URL**」按钮。token 每 boot 刷新，每次启动后重新抓。

**日志里的 token 会自动打码**（子进程入口行就地等长覆盖 + 管理器输出统一脱敏 + 每次启动全量清历史），所以日志可以放心外发排查。

---

## 10. 管理 app 说明（dsh_manager.pyw）

- 配置驱动：版本/路径/端口/浏览器全在 `config.json`（模板 `config/config.example.json`，支持 `<base>/<home>/<npm_global>` 占位符）。**最多 6 个槽**（`MAX_SLOTS = 6`，界面按"每槽两列"自适应排布；配多了启动即报错）。
- 全量日志：所有启动/停止/杀进程/开浏览器动作 + dsh stdout/stderr 写入 `dsh-manager.log`；**token 自动打码**，可放心外发。
- 子进程输出**直接写日志文件句柄，不走管道**：dsh 源码里没有任何 EPIPE 处理，父进程一退出管道读端关闭，正在跑的 dsh 会因写 stdout 而崩。
- 精准杀进程：按各槽 `bin.js` 签名清理（含外部拉起的同版本孤儿），不误伤其它 node；端口被**非 dsh** 进程占用时拒绝清理并告警。
- 窗口可缩放：拖边框内容跟随。
- 启动前端口预检；自动清理该槽的 `.credentials.yaml.lock` 死锁（PID 已死、或存活但不是本槽 dsh 才删）。
- 装/卸版本：底部「安装官方版 dsh」装新槽、「卸载 / 移出…」卸槽（卸载**一次一个**，先备份数据再删；另有「移出管理」只改 config、不删文件）；两者进行中所有按钮变灰。
- 维护与排查细节见 [`架构与维护说明.md`](架构与维护说明.md)；各槽实际取值看管理器卡片或 `config.json` 的 `versions` 段。

---

## 11. 卸载（极干净）

隔离安装的好处就是这一步：**删两个文件夹即净**，无全局改动、无注册表、无服务、无 PATH 软链。

```bat
rmdir /s /q <安装根>\dsh-012rc1
rmdir /s /q <安装根>\dsh-012rc1-home
```

- ❌ 绝不对隔离槽执行 `npm -g uninstall @deepseek-ai/dsh`（那会动 npm 全局那份）。
- ❌ 绝不删 `~/.dsh`（那是全局安装的数据家目录）。
- ✅ 只有真的要卸全局安装时才用 `npm -g uninstall`。

> ⚠️ 删 `dsh-012rc1-home`（数据目录）等于**丢掉会话、配置和凭据**。手工删之前先整个复制一份出来。
> 用管理器卸载的话，它会先把用户数据备份到 `<安装根>\dsh备份\`，再删程序产物。
