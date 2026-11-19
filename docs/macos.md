# macOS

dsh-tm supports Apple Silicon (arm64) and Intel (x64). The builds target macOS
13.5 and newer. The GitHub Actions smoke tests run on macOS 15 on both
architectures.

## 使用 App

1. 从同一个 GitHub Actions 运行下载对应架构的已签名 ZIP 与 SHA-256 清单。PR
   会记录最终产物的链接和验证结果。
2. 核对清单后解压，把 `DS-Manager.app` 移到「应用程序」。
3. 双击启动，选择「安装官方版 dsh」或纳入已有 npm 版。管理器内置 Python/Tk、
   Node、npm 和 pnpm。首次安装槽和初始化 web profile 需要连接 npm 仓库。
4. 用槽位卡片打开浏览器。需要从终端运行槽时，在槽目录运行
   `./dsh.command` 或 `./dsh-cli.command`。停止脚本会核对该槽的进程身份。

App 和源码默认共用 `~/Library/Application Support/DSH-TM/config.json`。
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
  停止入口使用管理器的进程身份检查，移动 App 后可从槽体检补上缺失的停止脚本。
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

## Signing setup for the upstream PR

Create the GitHub Environment `macos-signing`, restrict it to the reviewed
`codex/macos-support` branch and require a reviewer. Set the following secrets
in the environment. Encode the two binary/text credential files as base64
before pasting them into the GitHub secrets UI.

- `APPLE_CERTIFICATE_P12_BASE64` and `APPLE_CERTIFICATE_PASSWORD`.
- `APPLE_NOTARY_KEY_BASE64`, `APPLE_NOTARY_KEY_ID` and `APPLE_NOTARY_ISSUER_ID`.

Set the repository variable `DSH_TM_MACOS_SIGNING` to `true`, then trigger a
fresh push run on the trusted candidate branch. Review the run SHA before
approving the environment jobs. Both architecture jobs produce a ZIP and
SHA-256 after Developer ID signing, notarization, stapling and Gatekeeper
assessment. Signing waits for both macOS builds and the Windows check of the
same commit. The signing job uses the build artifacts from its own run.
