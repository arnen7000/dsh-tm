# What this PR does / 改动说明

<!-- One sentence on what problem this solves. Attach before/after screenshots for UI changes.
     一句话说清这个 PR 解决什么问题。涉及界面的请附截图（改前 / 改后）。 -->

## Changes / 改了什么

<!-- Bullet points, not a diff dump / 列要点，别贴 diff -->

-

## How it was verified / 怎么验证的

<!-- Tick only what you actually ran / 勾选你实际跑过的，不要勾没跑的 -->

- [ ] `scripts/check-all.ps1` is green — 6 static checks + 17 offline suites / 全绿（6 项静态检查 + 17 套离线回归）
- [ ] New or changed behaviour has regression assertions in `scripts/test-*.py` / 新增或修改的行为补了回归断言
- [ ] UI changes: launched the manager and clicked through it / 涉及界面：真启动管理器点过一遍
- [ ] Docs updated where relevant (README / 使用说明 / 架构说明) / 涉及文档：相关 `.md` 已同步

## Hard constraints / 硬约束自查

<!-- Each of these has caused a real incident once. See CONTRIBUTING.md.
     这几条踩过一次就会出事，详见 CONTRIBUTING.md。 -->

- [ ] Touched `.bat` files are still **GBK + CRLF + no BOM** (`scripts/fix-bat-encoding.py --check` passes) / 改动的 `.bat` 仍是 GBK + CRLF + 无 BOM
- [ ] No real usernames, absolute paths or tokens added to code or docs / 没有把真实用户名、绝对路径、token 写进代码或文档
- [ ] No new runtime dependency on `.ps1` scripts (install path must call npm from pure Python) / 没有新增对 `.ps1` 脚本的运行时依赖
- [ ] Slot paths still use placeholders (`<base>` / `<home>` / `<self>` / `<npm_global>`), no hard-coded drive letters / 槽路径仍用占位符，没写死盘符
- [ ] Delete/repair criteria were not loosened (better to delete too little than too much) / 没放宽删除或修复的判据

## Related issues / 关联 issue

<!-- e.g. Closes #12 / 例如 Closes #12 -->
