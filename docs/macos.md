# macOS

DSH-TM supports Apple Silicon (arm64) and Intel (x64). The builds target macOS
13.5 and newer. The GitHub Actions smoke tests run on macOS 15 on both
architectures.

## 使用 App

1. 在 [GitHub Releases](https://github.com/arnen7000/dsh-tm/releases/latest) 下载对应架构的
   App ZIP 和同名 `.sha256`。Apple Silicon 选 `macos-arm64`，Intel 选 `macos-x64`。
2. 核对清单后解压，把 `DSH-TM.app` 移到「应用程序」。`v0.1.11-preview.1` 的包内名称为 `DS-Manager.app`。
3. 双击启动，选择「安装官方版 dsh」或纳入已有 npm 版。管理器内置 Python/Tk、
   Node、npm 和 pnpm。首次安装槽和初始化 web profile 需要连接 npm 仓库。
4. 用槽位卡片打开浏览器。需要从终端运行槽时，在槽目录运行
   `./dsh.command` 或 `./dsh-cli.command`。停止脚本会核对该槽的进程身份。

GitHub 上名称带 `-unsigned` 的 ZIP 使用临时签名，尚未做 Developer ID 签名和 Apple 公证。
macOS 可能阻止首次启动。核对来源和 SHA-256 后，可按 [Apple 的打开方式](https://support.apple.com/zh-cn/102445)
为此 App 单独确认打开。

### 安装进行中

安装进度窗随管理器显示。「在后台继续」会隐藏进度窗，可从主界面的「安装进度」重新打开。
这期间已有槽仍可启动、停止、打开浏览器或复制入口。设置和更改槽会暂停。关闭主窗或用 Command-Q 退出时，管理器会提示先等待安装结束。
安装结束时刷新槽位，并在窗口和日志里汇总成功与失败。遇到下载超时可重试，未完成的安装会清除暂存文件。

维护者可在原生 Mac 上对解压后的包运行 `python3 scripts/test-macos-app.py --app /path/to/DSH-TM.app --dsh-version 0.2.2-rc.5`。
该检查使用隔离配置验证安装、认证访问、启停和端口释放。进度窗和后台队列可用 `python3 scripts/test-macos-install-gui.py` 单独回归。

App 和源码默认共用 `~/Library/Application Support/DSH-TM/config.json`。
首次启动 Web 可能需要安装插件。等待 20 秒后，管理器会显示启动等候超时，CLI 默认返回失败，
槽进程仍可在后台完成初始化。管理器核实槽进程开始监听后会显示运行中。也可以点「停止」取消。
关闭管理器后，槽可以继续运行。新开管理器时，保留运行的槽可能需要重新取得认证入口。
可以回到原来已登录的浏览器标签页，或停止并重新启动该槽后再点「打开浏览器」。
移走 App 不会删除槽和备份。旧工作区里的配置可以显式打开。先退出旧管理器，
使用原配置路径进行升级，启动后用「槽体检」确认入口、运行时和数据目录。

```sh
python3 dsh_manager.pyw --config /path/to/old/config.json
python3 dsh-tm.py --config /path/to/old/config.json doctor --json
```

## 与 Windows 的使用差异

- 两个平台共用六槽界面和 CLI JSON v1。安装、扫描和纳入、启停、体检与修复、
  移出和接回、卸载与搬迁都在相同菜单下。
- Windows 便携包从程序目录读配置。macOS 的配置与日志默认位于
  `~/Library/Application Support/DSH-TM`。打开槽目录和浏览器时调用 macOS 的默认程序。
- macOS 槽内使用 `.command`。启动和 CLI 入口使用槽内 Node，可在管理器关闭后运行。
  停止入口使用管理器的进程身份检查。移动 App 或从 `DS-Manager.app` 升级后，如果旧停止脚本仍指向原路径，
  将旧停止脚本移到别处，再用「槽体检 → 修复」补上缺失的停止脚本。
- Windows 可在体检中把旧 PATH Node 固定到槽内。macOS 新装槽自带 Node 和 npm，
  旧配置仍可使用共享 Node。迁移旧数据时保留来源目录后，可重新纳入已有安装。

## Run from source

Use Python 3.8+ with Tk 8.6+ and a native Node distribution with npm. Node 24
is recommended for current dsh versions. Ensure pnpm is available on PATH for
profiles that require it on first launch.

```sh
python3 scripts/check-env.py
python3 dsh_manager.pyw
python3 dsh-tm.py list --json
```

The packaged executable supports `--cli` and `--check-env` from Terminal.

## 后续版本的 Apple 签名

以新的版本号发布 Developer ID 签名包。v0.1.11 公开发布后，标签和已有下载保持可追溯。

在仓库的 **Settings → Environments** 创建 `macos-signing`。在 **Deployment
branches and tags** 选 **Selected branches and tags**，添加类型为 **Branch**、
名称为 `codex/macos-support` 的规则。在 **Required reviewers** 添加批准人。
启用 **Prevent self-review** 时，由另一位指定审阅人批准触发账号发起的任务。

在这个 Environment 的 **Environment secrets** 添加以下值。

| Secret | 值 |
| --- | --- |
| `APPLE_CERTIFICATE_P12_BASE64` | Developer ID Application 证书及其私钥导出的 `.p12`，用 `base64 -i /path/to/signing.p12 \| pbcopy` 复制编码值 |
| `APPLE_CERTIFICATE_PASSWORD` | 导出 `.p12` 时设置的密码 |
| `APPLE_NOTARY_KEY_BASE64` | App Store Connect Team API Key 的 `.p8`，用 `base64 -i /path/to/AuthKey.p8 \| pbcopy` 复制编码值 |
| `APPLE_NOTARY_KEY_ID` | Team API Key 对应的 Key ID |
| `APPLE_NOTARY_ISSUER_ID` | App Store Connect Team API Keys 页的 Issuer ID |

发布方在 `codex/macos-support` 推送一条以 `chore(release): request macOS signing`
开头的提交，触发正式签名运行。流程先核实 `macos-signing` 存在且已配置批准人。

Windows 便携包与两个架构的 macOS 构建通过后，打开该运行的 **Summary**，
在 **Review deployments** 勾选 `macos-signing`，核对提交 SHA，再点 **Approve and deploy**。
签名步骤使用这次运行的构建产物，完成 Developer ID 签名、公证和票据装订，再解压验证。

通过签名验收后合并 PR。若同一运行的发布任务已因等待合并而停止，重跑该发布任务。
发布任务校验 `main` 源码与已签名源码一致，核对三个 ZIP 的校验值，然后创建正式 GitHub Release。
