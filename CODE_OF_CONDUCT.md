# Contributor Code of Conduct / 贡献者行为准则

> Adapted from [Contributor Covenant v2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).
> 适配自 [Contributor Covenant v2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/)，
> 保留核心条款，并按本项目的实际情况（以 Windows 桌面工具为主、单人维护起步）做了简化。

---

## English

### Our Pledge

We as members, contributors, and maintainers pledge to make participation in our project
a harassment-free experience for everyone, regardless of age, body size, visible or invisible
disability, ethnicity, sex characteristics, gender identity and expression, level of experience,
education, socio-economic status, nationality, personal appearance, race, religion, or sexual
identity and orientation.

### Our Standards

Examples of behavior that contributes to a positive environment:

- Using welcoming and inclusive language
- Being respectful of differing viewpoints and experiences
- Gracefully accepting constructive criticism
- Focusing on what is best for the community
- Showing empathy towards other community members

Examples of unacceptable behavior:

- The use of sexualized language or imagery, and sexual attention or advances of any kind
- Trolling, insulting or derogatory comments, and personal or political attacks
- Public or private harassment
- Publishing others' private information, such as a physical or email address, without their explicit permission
- Other conduct which could reasonably be considered inappropriate in a professional setting

### Project-specific notes

- **Do not paste raw log output into issues or pull requests.** The manager's log contains your
  Windows username and absolute paths, and the `dsh备份` backup folder may contain plaintext
  credentials. Redact before posting: replace the username with `<用户名>` and drive paths with
  `<home>` / `<base>`.
- **Security issues** (credential leaks, unintended file deletion, etc.) must **not** be filed as
  public issues. Report them privately as described in [SECURITY.md](SECURITY.md).
- The `.bat` files in this project must stay **GBK + CRLF + no BOM**. This is a hard constraint,
  not a style preference: UTF-8 makes adjacent Chinese `REM` lines tear apart, which yields
  `rc=255` and a window that flashes and closes. Read [CONTRIBUTING.md](CONTRIBUTING.md) before
  touching that code.

### Enforcement Responsibilities

Maintainers are responsible for clarifying and enforcing these standards and will take appropriate
and fair corrective action in response to any behavior that they deem inappropriate, threatening,
offensive, or harmful. Maintainers have the right and responsibility to remove, edit, or reject
comments, commits, code, issues, and other contributions that are not aligned with this Code.

### Scope

This Code of Conduct applies within all project spaces (issues, pull requests, discussions,
commit messages), and also applies when an individual is officially representing the project in
public spaces.

### Enforcement

Instances of abusive, harassing, or otherwise unacceptable behavior may be reported privately to
the maintainers via the contact listed on their GitHub profile. All complaints will be reviewed
and investigated promptly and fairly. Maintainers are obligated to respect the privacy and
security of the reporter of any incident.

### Attribution

This Code of Conduct is adapted from the [Contributor Covenant](https://www.contributor-covenant.org),
version 2.1, available at [contributor-covenant.org/version/2/1/code_of_conduct](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).
The Covenant text is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

---

## 中文

### 我们的承诺

为了让每个人都能在没有骚扰的环境里参与，我们承诺：无论年龄、体型、可见或不可见的残障、
族裔、性别认同与表达、经验水平、教育程度、社会经济状况、国籍、外貌、种族、宗教或性取向如何，
参与本项目都不会受到骚扰。

### 我们的标准

有助于营造良好环境的行为：

- 使用友善、包容的语言
- 尊重不同的观点和经验
- 平和地接受建设性批评
- 关注对社区最有利的事
- 对其他参与者表示同理

不可接受的行为：

- 性化的语言或图像，以及任何形式的性关注或挑逗
- 挑衅、侮辱、贬损的评论，人身攻击或政治攻击
- 公开或私下的骚扰
- 未经明确许可，发布他人的私人信息（如真实姓名、住址、邮箱）
- 其他在专业场合可合理认定为不当的行为

### 关于本项目的特别说明

- **不要在本仓库的 issue / PR 里粘贴真实日志原文**。管理器日志里含 Windows 用户名和绝对路径，
  `dsh备份` 目录里可能有明文凭据。提交前请脱敏：把用户名换成 `<用户名>`，盘符路径换成
  `<home>` / `<base>`。
- 安全问题（凭据泄露、误删用户数据等）**不要开公开 issue**，请按 [SECURITY.md](SECURITY.md)
  里的方式私密报告。
- 本项目的 `.bat` 文件必须保持 **GBK + CRLF + 无 BOM**。这不是风格偏好而是硬约束：
  UTF-8 会让相邻中文 `REM` 撕裂，导致 `rc=255`、双击闪退。改动相关代码前请先读
  [CONTRIBUTING.md](CONTRIBUTING.md)。

### 执行责任

项目维护者有责任澄清并执行上述标准，并对任何不当行为采取适当且公正的纠正措施，
包括删除、编辑或拒绝不符合本准则的评论、提交、代码、issue 及其他贡献。

### 适用范围

本准则适用于所有项目空间（issue、PR、讨论区、提交信息），也适用于个人在公共场合
正式代表本项目时的行为。

### 执行

如遇滥用、骚扰或其他不可接受的行为，请通过维护者 GitHub 主页所列联系方式私下反映。
所有投诉都会被及时、公正地审查与处理。维护者有义务尊重举报者的隐私与安全。

### 归属

本准则适配自 [Contributor Covenant](https://www.contributor-covenant.org) 2.1 版，
该文本采用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 许可。
