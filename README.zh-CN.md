# 黎曼假设 · Lean 复验

[![验证：4/4 通过](https://img.shields.io/badge/verification-4%2F4%20passed-brightgreen)](results/status.json)
[![Lean：4.33.1](https://img.shields.io/badge/Lean-4.33.1-blue)](results/environment.json)
[![证据：已归档](https://img.shields.io/badge/evidence-archived-blue)](results/final/20261121T060158Z-a03969de/manifest.json)

[English](README.md) | **简体中文**

> **复验已完成。** 全项目构建成功，最终证明通过 Mathlib 标准黎曼假设陈述的类型检查，Lean 内核重新检查通过。

本仓库记录了对 **Levent Alpöge 的 Lean 形式化证明**的复现与审计，公开运行脚本、服务器加载器修复和完整验核记录，方便直接查看、核对与复现。

**175,187 个 Lean 模块 · 5 个最终定理 · 4/4 项验证通过**  
完成时间：**2026-11-25 07:18（北京时间）**，即 2026-11-24 23:18 UTC。

**[验证结果](results/status.json) · [归档证据](results/final/20261121T060158Z-a03969de/) · [复现方法](#如何复现) · [原始源码](https://github.com/CocoSgt/riemann-hypothesis-lean/tree/7cbf7a0c32fca1badf05931879a37c3f8a2b03b4)**

## 已完成的成果

| 检查 | 结果 | 实际检查了什么 |
|---|---|---|
| [全项目构建](results/logtails/20261121T060158Z-a03969de/build-build-main.log.tail.txt) | **通过** | 完成 `Main` 及其全部传递依赖的构建，覆盖提交中的 175,187 个模块。 |
| [最终定理检查](results/evidence/20261121T060158Z-a03969de/build-Check.lean) | **通过** | 显式检查证明具有 Mathlib 标准 `RiemannHypothesis` 类型，并打印五个最终定理的公理依赖。 |
| [公理白名单检查](results/logtails/20261121T060158Z-a03969de/build-validate-axioms.log.tail.txt) | **通过** | 五个最终定理均只依赖允许的三项标准公理。 |
| [全新环境内核重放](results/evidence/20261121T060158Z-a03969de/build-fresh-replay.command.sh) | **通过** | 使用 `leanchecker --fresh -v Main` 重新检查 `Main`；本阶段耗时 **17 小时 7 分钟**。 |

[公理报告](results/logtails/20261121T060158Z-a03969de/build-axioms.log.tail.txt)中，五个最终定理都只列出 `propext`、`Classical.choice` 和 `Quot.sound`：**没有额外公理或 `sorryAx` 依赖**。[原始证明源码保持不变](results/evidence/20261121T060158Z-a03969de/build-final-source-status.txt)。

与标准命题的显式类型检查只有两行：

```lean
import Main
example : _root_.RiemannHypothesis := _root_.riemannHypothesis
```

## 直接查看证据

- **[最终状态](results/status.json)：** 完成状态、各阶段退出码、起止时间和资源采样。
- **[完整归档与 SHA-256 清单](results/final/20261121T060158Z-a03969de/manifest.json)：** 84 个压缩文件，包含日志、命令、计时、来源记录和源码差异；清单记录每个归档分块的大小与校验和。
- **[运行环境](results/environment.json)与[工具链来源记录](results/evidence/20261121T060158Z-a03969de/build-toolchain-provenance.json)：** 实际机器限制、工具链选择及二进制哈希。
- **[初始源码审计](STATIC_AUDIT.md)：** 模块数量、导入图、定理陈述检查及早期发现；该记录写于完整复验完成之前。

完整结果保存在提交 [`c441248e`](https://github.com/sunnyspot114514/riemann-lean-verification-20261121/tree/c441248e463b0acc0d385c59631b1d8ffd83842e)。运行编号：`20261121T060158Z-a03969de`。

## 服务器配置与加载器修复

本次复验在以下[记录在案的配置](results/environment.json)上完成：

| 资源 | 配置 |
|---|---|
| CPU | 32 个逻辑 CPU |
| 内存 | 60 GiB cgroup 上限 |
| 数据盘 | [500 GiB 容量](results/status.json) |
| 构建并发 | 8 个 Lake worker，每个 worker 使用一个 Lean 线程 |
| 验核运行时 | Lean 4.33.1，使用已公开的加载器修复 |

平台将 `vm.max_map_count` 固定为 `65530`，Lean 同时映射大量模块文件时会触及上限。我们为加载器增加了有界映射和磁盘后备内存区，并清理已完成的 `.setup.json` 中间文件，最终在这台租用服务器上完成构建与内核重放。

加载器修复通过 **[29/29 项回归检查](results/loader-fix/20261121T060024Z-523a39ce/test-report.json)**。[构建清单](results/evidence/20261121T060158Z-a03969de/build-loader-build-manifest.json)记录了内核归档对象保持不变的检查结果。实现和诊断见[补丁](patches/lean-4.33.1-bounded-mmap-arena.patch)、[设计与检查说明](patches/ARENA-NOTES.md)及[原始失败分析](results/diagnostics/20261121-map-limit/README.md)。

## 如何复现

仓库保留了完整工作流程：

| 入口 | 用途 |
|---|---|
| [`scripts/bootstrap.sh`](scripts/bootstrap.sh) | 获取并核对固定版本的工具链、源码、依赖及 Mathlib 缓存。 |
| [`scripts/build_lean_loader.py`](scripts/build_lean_loader.py) / [`scripts/test_lean_loader.py`](scripts/test_lean_loader.py) | 构建独立安装的修复版运行时，并执行加载器检查。 |
| [`scripts/run_all.py`](scripts/run_all.py) | 执行各项验核、采样资源并发布证据。 |
| [`scripts/clean_completed_setups.py`](scripts/clean_completed_setups.py) | 在大规模构建期间清理已完成的 setup 中间文件。 |

<details>
<summary><strong>本次运行采用的启动配置</strong></summary>

先准备固定版本的源码与工具链，包括独立安装的 `lean-4.33.1-loader-v2` 及其构建清单。运行器沿用原服务器目录结构，使用 `tmux`、位于 `127.0.0.1:20080` 的本地 HTTP 代理，以及发布证据所需的 Git 远程仓库。在自己的服务器上运行前，请配置对应路径、网络和发布目标。

```bash
python3 /root/autodl-tmp/rh-check/verification/scripts/run_all.py \
  --base /root/autodl-tmp/rh-check \
  --toolchain-dir /root/autodl-tmp/rh-check/tools/lean-4.33.1-loader-v2 \
  --olean-arena-dir /root/autodl-tmp/rh-check/tmp/olean-arena \
  --mmap-regions 60000 \
  --lean-num-threads 8 \
  --resumed-from-run-id 20261120T184814Z-ebb611be
```

最后一个选项记录本次接续的中断运行；续跑时替换为自己的先前运行编号，新运行则省略。准备阶段所需程序与具体执行命令见脚本及[证据目录](results/evidence/20261121T060158Z-a03969de/)。

</details>

## 固定版本与致谢

| 组件 | 固定版本 |
|---|---|
| 证明源码，由 CocoSgt 镜像 | [`7cbf7a0c32fca1badf05931879a37c3f8a2b03b4`](https://github.com/CocoSgt/riemann-hypothesis-lean/tree/7cbf7a0c32fca1badf05931879a37c3f8a2b03b4) |
| Mathlib | [`0df444a360eaa60ab8c11dca51a86af692955474`](https://github.com/leanprover-community/mathlib4/tree/0df444a360eaa60ab8c11dca51a86af692955474) |
| Lean 基础发行版 | [`4.33.1`](https://github.com/leanprover/lean4/releases/tag/v4.33.1)，使用记录在案的加载器补丁 |

数学形式化证明归 **Levent Alpöge**，见[作者公告](https://alpo.ge/remark.pdf)。本仓库的贡献是复验流程、平台适配与公开运行证据。

## 验证范围与局限

- **运行时：** 成功运行使用了已公开的加载器修复。原始证明源码和内核归档对象保持不变，但这是重新链接的运行时，并非全程仅用原版发行二进制完成。没有执行完整编译器 bootstrap 或全部上游测试；详见[加载器构建清单](results/evidence/20261121T060158Z-a03969de/build-loader-build-manifest.json)。
- **检查器：** fresh replay 使用同一个 Lean 内核实现。本次没有运行 `nanoda`、`con-leche` 或 `con-ron` 等独立检查器。
- **复现与资源：** 结果对应固定源码提交。构建采用增量续跑，并使用官方 Mathlib 缓存。耗时不是冷缓存基准；500 GiB 是分配的数据盘容量，不是测得的峰值用量或适用于所有复现的最低要求。
- **证据范围：** 归档包含验核记录，不包含整个编译产物或独立证明导出。它记录本次实际完成的形式化检查；面向人工阅读的完整数学论证不在本仓库范围内。
