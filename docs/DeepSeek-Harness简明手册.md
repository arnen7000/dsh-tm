# DeepSeek Harness（dsh）简明使用手册

> 想 10 分钟跑起来并日常使用 dsh（DeepSeek Harness）？看这份就够。想深入配置/插件体系，请看配套的《DeepSeek-Harness使用手册.md》（完整版）。
>
> 本文不涉及任何特定机器/用户的私有配置，命令中的 `%USERPROFILE%` 泛指你的用户主目录。

---

## 1. 三句话认识 dsh

- dsh 是 DeepSeek 推出的 **Agent 运行时**：你在**网页对话框**里给它派活，它能写代码、跑命令、查资料，并把过程完整展示给你。
- 核心理念 **"一切皆插件"**：聊天界面、Agent 能力、第三方增强，都是可插拔的插件组合。
- 通过 `dsh web` 启动网页版（默认 http://127.0.0.1:3080），也可以 `headless` 无头跑一次性任务。

---

## 2. 安装（任选其一）

```bash
# 方式一：不装，临时跑一次（新手最快）
npx -y @deepseek-ai/dsh web

# 方式二：全局安装（推荐日常用）
npm install -g @deepseek-ai/dsh
dsh web
```

前置要求：**Node.js ≥ 22.19**（`node --version` 检查）。低于此版本会直接报错。

> 💡 Windows 用户注意：PowerShell 里敲 `dsh` 若提示脚本被禁用，改用 **`dsh.cmd`** 即可。

---

## 3. 日常用法

| 你想… | 怎么做 |
|---|---|
| 打开网页版 | `dsh web` → 浏览器访问 `http://127.0.0.1:3080` |
| 换模型 / 思考强度 | 网页设置里改；或编辑 `%USERPROFILE%\.dsh\settings.yaml` 的 `agent-default-model` |
| 一次问答，不要界面 | `dsh --profile headless "帮我总结这个文档"` |
| 装个插件 | 见下文第 4 节 |
| 看当前生效配置 | `dsh --dump-config` |

常用模型：`deepseek-v4-flash`（默认，性价比）/ `deepseek-v4-pro`（旗舰更强）。

---

## 4. 装插件（三步）

以社区侧边栏插件 `dsh-better-sidebar` 为例。**先备份**再动手：

1. **加依赖**：编辑 `%USERPROFILE%\.dsh\profiles\web\package.json` 的 `dependencies`：
   ```json
   "dependencies": { "dsh-better-sidebar": "^0.12.3" }
   ```
2. **挂载声明**：编辑同目录 `cordis.patch.yml`，追加：
   ```yaml
   - insert:
       - id: better-sidebar
         name: dsh-better-sidebar
   ```
3. **安装并重启**：
   ```bash
   cd %USERPROFILE%\.dsh\profiles\web && pnpm install
   dsh web
   ```

> 若 `pnpm` 不存在：先 `npm install -g pnpm`。用 `github:作者/仓库` 方式安装的插件**不会**自动写挂载行，必须手动补第 2 步。

---

## 5. 常见问题速查

| 现象 | 处理 |
|---|---|
| `dsh` 不是有效命令 | 没装上 / PATH 没刷新，重开终端或全局安装 |
| 浏览器打不开 3080 | 端口被占：`netstat -ano \| findstr 3080` 找 PID 后结束 |
| 模型没反应 | 检查 API Key 与 `settings.yaml` 的模型配置 |
| 插件装完不生效 | 看 `cordis.patch.yml` 的 id/name 是否拼对 |
| 报 zstd / AbortSignal 错误 | Node 版本 < 22.19，升级 Node |

---

## 6. 数据与目录

- 所有个人数据（会话、配置、插件）都在 **`%USERPROFILE%\.dsh`**（即 `$DSH_HOME`），**删掉 dsh 程序不影响这些数据**；想备份/迁移，拷这个目录即可。
- `profiles/web/` 是网页版 profile：`package.json`（插件清单）+ `cordis.patch.yml`（你的配置声明）+ `cordis.yml`（生成文件，别手改）。

---

## 7. 更多资料

- 完整手册：`DeepSeek-Harness使用手册.md`（同目录）
- 官方仓库：https://github.com/deepseek-ai/DeepSeek-Harness
- 官方文档站：https://deepseek-harness.github.io/deepseek-harness/