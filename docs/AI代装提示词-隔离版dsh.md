# AI 代装提示词 · 隔离版 dsh（复制即用）

> **用途**：想让 AI 助手（dsh 会话里的智能体 / 其它编码助手）替你安装一个新的 dsh **隔离版本槽**时，
> 把下方代码块整段复制发给它，把 `<目标版本号>` 换成目标版本即可。
>
> **什么时候用**：
> - 新增一个槽位（现役 v1/v2/v3 之外，如装个 v4 尝鲜新版）；
> - 换新机器后按本机约定重建整套多槽部署。
>
> **什么时候不用**：已有槽想换版本 —— 管理器里「卸载 / 移出…」+「安装官方版 dsh」即可
>（2026-10-30 起「换版本」按钮已移除）。安装流程内置：版本存在性预检、手误版本号自动纠正
>（`0.2.0rc.2`→`0.2.0-rc.2`）、默认 npm 源网络故障自动回退 npmmirror 镜像。

---

## 使用方法

1. 复制下方代码块，把 `<目标版本号>`（形如 `0.2.0-rc.2`，**注意连字符**）和 `<新槽序号>`（如 `4`）替换掉；
2. 粘贴给 AI；
3. AI 应当**先给安装计划再动手**——如果它跳过计划直接改文件，把第 9 条再发一遍给它。

---

## 提示词正文（整段复制）

```text
请帮我在本机安装 DeepSeek Harness (dsh) 的隔离版本 <目标版本号>（npm 包名 @deepseek-ai/dsh），
作为新槽 v<新槽序号> 并入 <管理器目录> 管理体系。

【必须遵守的约定】
1. 五隔离轴（违反任何一条都会与现有版本冲突/腐蚀）：
   - 程序隔离：装到独立目录 <安装根>\dsh-versions\dshV<新槽序号>\，绝不 npm install -g；
   - 数据隔离：DSH_HOME 指向 <安装根>\dsh-versions\dshV<新槽序号>\.dsh-home
     （每槽独立数据，各自登录、各装各的插件，互不可见）；
   - 运行时隔离：每槽一份 node，放在 <槽目录>\node\node.exe（≥22.19），
     安装与运行都用这一份，保证 node-pty 等原生模块按同一 ABI 编译/运行；
   - 端口隔离：按槽序千位递增 —— 4080 / 5080 / 6080 / 7080 / 8080 / 9080（第 N 槽 = 4080+1000×(N-1)），
     **唯一判据是「该端口此刻有没有被别的程序占用」**：被占就向后顺延，不特意避开某个号段；
   - 临时目录隔离：TEMP 指向 <安装根>\dsh-versions\dsh-temp\dshV<新槽序号>。
2. 安装方式：用该槽自己的 node 直跑 npm-cli.js（不要调 npm.cmd / npm.ps1），在槽目录内执行：
     cd <安装根>\dsh-versions\dshV<新槽序号>
     ..\dshV<新槽序号>\node\node.exe ..\dshV<新槽序号>\node\node_modules\npm\bin\npm-cli.js init -y
     ..\dshV<新槽序号>\node\node.exe ..\dshV<新槽序号>\node\node_modules\npm\bin\npm-cli.js install @deepseek-ai/dsh@<目标版本号>
   （把相对路径换成该槽 node 的绝对路径即可。）
   遇到网络类失败（npm 退出码为负数，如 -4077=ECONNRESET 连接被重置）时，追加
   --registry=https://registry.npmmirror.com 重试（仅命令级参数，不改任何全局 npm 配置）。
3. 版本号规范：npm semver 预发布号带连字符——是 0.2.0-rc.2，不是 0.2.0rc.2。
   装前先 npm view @deepseek-ai/dsh versions 确认目标版本存在。
4. 端口钉法：启动参数带 --host 127.0.0.1 --port <分配端口> --no-open；
   若该版本 web 启动器报 unknown option '--port'，改把端口钉在
   <DSH_HOME>\profiles\web\cordis.patch.yml 的 webserver 段 port: <分配端口>
   （配置叠加顺序 bundle → profile patch → home patch → --patch，最后写入的层获胜）。
5. 管理器注册：改 <管理器目录>\config.json 前先备份为 config.json.bak-<时间戳>，
   再在 versions 下新增 v<新槽序号> 节点，字段必须齐全：
   label（= dshV<新槽序号>）/ node_exe / bin_js / dsh_home / temp_dir / port / browser / extra_args
   （extra_args = ["web","--host","127.0.0.1","--port","<分配端口>","--no-open"]）。
   写入方式：临时文件 + 原子替换，UTF-8 无 BOM。
   **四个路径字段一律写 `<base>` 占位符**（<base> = 安装根），不要写死盘符或用户名。
6. 启动器：槽目录下生成三件 bat —— dsh.bat / dsh-cli.bat / kill-<分配端口>.bat。
   格式照抄现有槽，只改 node 路径与端口，不要自创格式。
   **bat 必须是 GBK 编码 + CRLF 行尾 + 无 BOM**，且里面写的是展开后的绝对路径（cmd 不认占位符）。
7. 装完必须逐项验证：
   - <槽目录>\node_modules\@deepseek-ai\dsh\package.json 的 version 字段 == <目标版本号>；
   - 启动后控制台打印 http://127.0.0.1:<分配端口>/?token=... 入口 URL
     （0.1.5+ 必须 token，裸开端口会 401）；
   - netstat -ano 确认 <分配端口> 处于 LISTENING；
   - 槽形状完整：自带 node、bin.js 存在、四个路径是占位符、temp_dir 独立、三件 bat 齐全。
8. 治理红线：
   - 绝不触碰其它槽的目录 / 数据 / 端口 / 进程；
   - 绝不对隔离版执行 npm -g install / npm -g uninstall；
   - 删除或覆盖任何文件前，先列出清单征得我确认；
   - 全程把关键命令与结果留痕（日志或返回摘要），结论要能落到具体路径或实测输出。

请先输出一份安装计划（目录布局、node 来源与版本、端口、将要新建/修改的文件清单），
我确认后再动手。
```

---

## 附：给 AI 的兜底坑位（装不上时按此排查）

| 症状 | 根因与处置 |
|---|---|
| `npm error errno -4077 / ECONNRESET` | 官方源连接被重置（国内网络常见）。加 `--registry=https://registry.npmmirror.com` 重试 |
| `EPERM, rmdir ...node_modules\...` | 杀毒软件实时防护或运行中的进程锁文件。暂停防护 / 停掉该槽 dsh 再重试 |
| `atomic-write: timed out waiting for the writer lock` | 残留死锁。确认无 dsh 进程后删 `<DSH_HOME>\.credentials.yaml.lock` |
| `EADDRINUSE` | 端口被占。`netstat -ano | findstr :<端口>` 找 PID 后 `taskkill /PID <pid> /T /F` |
| `unknown option '--port'` | 该版本 web 启动器不转发 --port → 按 §5 钉进 profile 的 cordis.patch.yml |
| `node:zlib.createZstdDecompress` 不存在 | node < 22.19。换 ≥22.19 的 node |
| 启动 401 | 0.1.5+ 需要 token，用启动时打印的带 `?token=` 完整 URL |

更完整的排障手册：[isolated-install-runbook.md](isolated-install-runbook.md)
