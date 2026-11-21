# 版本与发布

> 对标版本：Vela v0.1.0 · 规则自 2026-11-21 起生效

## 1. 版本号规则

格式 `x.y.z`，只允许 `x.y.z` 三个数字；预发布后缀 `-dev.<run_number>` 由 CI 注入，
不写进源码版本号。

| 变更类型 | 递增位 | 示例 |
| --- | --- | --- |
| 修复、补丁、小功能、小幅度性能优化 | `z` | `0.1.0 → 0.1.1` |
| 一般功能更新（feat）、大幅性能优化 | `y`（`z` 归零） | `0.1.4 → 0.2.0` |
| 重构、破坏性更新 | `x`（`y`、`z` 归零） | `0.2.7 → 1.0.0` |

**禁止进一单位**：`z` 不向 `y` 进位。

- `0.1.9` 的下一个补丁是 `0.1.10`，不是 `0.2.0`；
- 连续 21 次补丁是 `0.1.21`，不是 `0.2.1`。

版本号唯一来源是 `vela/__init__.py` 与 `pyproject.toml`，两者必须一致：

```powershell
python tools\version.py show          # 打印当前版本
python tools\version.py bump z        # 补丁
python tools\version.py bump y        # 功能
python tools\version.py bump x        # 重构/破坏性
python tools\version.py set 1.0.0     # 直接设定
```

## 2. 分支模型

| 分支 | 角色 | 允许的来源 |
| --- | --- | --- |
| `develop` | 开发版，日常提交与 PR 的目标 | feature/fix 分支、直接提交 |
| `master` | 稳定版（stable），只接受来自 `develop` 的合并 | `develop` |

流程：

1. 日常改动提交到 `develop`；
2. `develop` 的 push 通过全量回归后，自动发布 `x.y.z-dev.<run_number>` 预发布；
3. 需要发稳定版时，把 `develop` 合并到 `master`；
4. `master` 的 push 通过回归后，自动发布 `x.y.z` 正式版并标记 Latest。

PR 到 `develop` 或 `master` 都会触发 CI（不含发布）。

## 3. 自动发布

工作流：

- [regression.yml](../.github/workflows/regression.yml)：Windows runner，CPython 3.13 / 3.15
  双矩阵，跑 44 个 unittest、11 个差分语料、构建并运行原生 exe、4 个基准输出校验；
- [ci.yml](../.github/workflows/ci.yml)：PR 触发上面的回归；
- [release.yml](../.github/workflows/release.yml)：`develop` / `master` push 触发回归 + 发布。

| 分支 | 版本 | Tag | Release 类型 |
| --- | --- | --- | --- |
| `develop` | `<base>-dev.<N>` | `v<base>-dev.<N>` | Pre-Release |
| `master` | `<base>` | `v<base>` | Latest |

- `<base>` 是仓库里的 `x.y.z`，即"下一个待发布的稳定版本"，不是当前 Latest；
- `N` 在同一 base 内从 1 开始递增；base 变化时重置为 1。
  例如 Latest 为 `v0.1.0`、源码版本已 bump 到 `0.1.1` 时，开发版为
  `v0.1.1-dev.1`、`v0.1.1-dev.2`……；等 `v0.1.1` 发布后源码 bump 到 `0.1.2`，
  开发版重新从 `v0.1.2-dev.1` 开始；
- `N` 由已有 tag 计算（`tools/dev_version.py`），不使用全局 run_number，
  失败的运行不会造成逻辑序号跳号；
- 开发分支的源码版本必须领先于 Latest：若 `x.y.z` 已经是 Latest，push `develop`
  会被 workflow 拒绝。稳定版发布后，必须先把源码版本 bump 到下一个版本
  （例如 `0.1.1 → 0.1.2`）再继续开发；
- 如果同名 Release 已存在，发布步骤会跳过（不覆盖、不重复打 tag）；
- 发布使用 `GITHUB_TOKEN`，不需要额外 secret。

- 每次发布新的 dev Pre-Release 后，自动删除上一个 dev Release 及其 tag，仓库只保留最新一个 dev 版本；

## 4. 发布产物

每个 Release 都包含：

| 产物 | 说明 |
| --- | --- |
| Source code (zip/tar.gz) | GitHub 自动生成 |
| `vela-<version>-windows-x86_64.tar.gz` | 编译器 + examples + docs + LICENSE |
| `vela-<version>-setup.exe` | Inno Setup 6 安装包，按当前用户安装 |
| `*.sha256` | 上述两个产物的校验和 |

安装包行为：

- 安装到 `%LOCALAPPDATA%\Vela`，不需要管理员权限；
- 把安装目录写入当前用户的 `PATH`，`vela build` 可直接使用；
- 卸载时移除目录与 PATH 项；
- 运行要求：Windows x86-64、Python 3.10+（`python` 在 PATH 中）。

## 5. 手动打包

```powershell
# 完整打包（本机需安装 Inno Setup 6）
powershell -File tools\package.ps1 -Version 0.1.1 -OutDir dist

# 只打 tar.gz（无 Inno Setup 时）
powershell -File tools\package.ps1 -Version 0.1.1 -OutDir dist -SkipSetup
```

CI 上 Inno Setup 6 已预装于 `windows-latest`；`ISCC.exe` 缺失时脚本会明确报错，
不会静默跳过 setup.exe。

## 6. 首次迁移（一次性）

仓库当前只有 `main`（v0.1.0 已发布）。迁移步骤：

```powershell
git branch master main          # master = 当前稳定版
git switch -c develop main      # develop 从同一提交开始
# 在 develop 上提交流程改动并把版本 bump 到 0.1.1
git push -u origin master
git push -u origin develop
# GitHub 仓库设置里把默认分支改为 develop，然后删除 main
```

`master` 的第一次 push 会尝试发布 `v0.1.0`；因为该 Release 已存在，发布步骤会跳过。
下一次 `develop → master` 会发布 `v0.1.1`（含 tar.gz 与 setup.exe）。

## 7. 红线

- 不手工创建/移动 `v*` tag 来绕过 CI；
- 不在 `develop` 上打正式版 tag；
- 不让源码版本号与 tag 不一致；
- 不跳过 `setup.exe`（Inno Setup 缺失时必须失败，而不是只发 tar.gz）。


## 8. 本地构建（-local.N）

每完成一轮本地改动，运行：

```powershell
python tools\local_build.py
```

规则：

- base = 远端最新 Pre-Release（例如 `0.3.0-dev.1`）；
- 本地版本 = `<base>-local.<N>`，每次构建 N 递增；
- 远端出现新的 Pre-Release 后 base 变化，N 重置为 1；
- 构建产物：`dist\vela.cmd` + `dist\local\vela\`（已注入本地版本号）；
- 机器 PATH 已包含 `I:\Project\Vela-Lang\dist`，任意目录可直接运行 `vela`；
- 当前 shell 的 PATH 若早于该配置，需要重开终端或临时前置。

`vela version` 会打印本地版本，例如 `0.3.0-dev.1-local.2`。
