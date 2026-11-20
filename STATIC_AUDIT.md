# 静态审计记录

报告日期：2026-11-21（北京时间）。对象为镜像仓库固定提交 [`7cbf7a0c32fca1badf05931879a37c3f8a2b03b4`](https://github.com/CocoSgt/riemann-hypothesis-lean/tree/7cbf7a0c32fca1badf05931879a37c3f8a2b03b4)。本文记录源码检查，**不表示完整构建或证明验证已经成功**；运行进展、各阶段退出码及结果以 [results/status.json](results/status.json) 为准。

## 实际证明目标

已对照指定 Mathlib 提交中的 [RiemannHypothesis 定义](https://github.com/leanprover-community/mathlib4/blob/0df444a360eaa60ab8c11dca51a86af692955474/Mathlib/NumberTheory/LSeries/RiemannZeta.lean#L182-L186)：排除负偶整数平凡零点及 `s = 1` 后，ζ 的零点满足 `s.re = 1/2`。仓库顶层没有把 RH 改成自定义的弱命题，也没有可见的额外定理参数。

[Main.lean](https://github.com/CocoSgt/riemann-hypothesis-lean/blob/7cbf7a0c32fca1badf05931879a37c3f8a2b03b4/src/Main.lean) 导出五项结果；其它目标的定义见 [Statements.lean](https://github.com/CocoSgt/riemann-hypothesis-lean/blob/7cbf7a0c32fca1badf05931879a37c3f8a2b03b4/src/Statements.lean)。

| 定理 | 声称的结论 |
|---|---|
| `riemannHypothesis` | Mathlib 的标准 RH |
| `riemannZeta_eq_zero_re` | 同一 ζ 零点结论 |
| `LFunction_ne_zero_of_half_lt_re` | 任意 Dirichlet 特征在实部大于 1/2 处非零，排除主特征的 `s = 1` |
| `LFunction_eq_zero_re` | 任意 Dirichlet 特征在临界带内的零点位于临界线 |
| `LFunction_eq_zero_re_of_isPrimitive` | 上述结论的本原特征版本 |

这里的 GRH 范围是 **Dirichlet L-functions**，不是所有类型的 L-functions。顶层从右半平面非零性推出 RH 的步骤使用函数方程等常规转换；困难集中在非零性证明及其庞大依赖中。

沿主链看到的条件定理随后有相应证明项传入，不能仅凭中间存在假设参数就认定最终定理仍带条件。反过来，函数名叫“已发表结果”也不保证它已经被正确形式化；仍需检查对应证明项。

## 已完成的静态检查

- 扫描的源码共有 **175,187 个 Lean 模块、17,374,215 行**；静态 `import` 图中全部属于 `Main` 的传递依赖，没有发现缺失的项目内导入或循环导入。
- 文本扫描与重点审阅没有发现能直接判定失败的 `sorry`、自定义公理或内核绕过证据。此结论不等于完成了所有证明项的类型检查。
- [CutCptCopy.lean](https://github.com/CocoSgt/riemann-hypothesis-lean/blob/7cbf7a0c32fca1badf05931879a37c3f8a2b03b4/src/Formalism/HermitianCurvature/HermitianCurvatureProofs/CutCptCopy.lean) 会改名、替换并复制声明，但调用的是正常的 `addDecl` 路径；不能仅凭存在元编程就认定绕过内核。对应实现见官方 [Lean/AddDecl.lean](https://github.com/leanprover/lean4/blob/v4.33.1/src/Lean/AddDecl.lean)。
- 重复定理名曾引起疑问，已用官方 Lean 4.33.1 的最小样例核验：不同模块允许证明同名同类型定理，证明项可以不同；这条线索没有构成错误证据。官方 [Environment.lean](https://github.com/leanprover/lean4/blob/v4.33.1/src/Lean/Environment.lean) 明确解释了该合并规则。
- 镜像 [README](https://github.com/CocoSgt/riemann-hypothesis-lean/blob/7cbf7a0c32fca1badf05931879a37c3f8a2b03b4/README.md) 的宏统计不完整：包含 `local`、`scoped` 声明的扫描口径得到 **67 个文件、91 项语法／宏／elab 声明**，而 README 报告 22 个文件。统计需要更正，但这个差异本身不是数学反证。

## 什么才算进一步核验

需要固定源码、官方工具链和依赖后完成构建；对五个最终定理检查公理依赖；确认其只使用 `propext`、`Classical.choice`、`Quot.sound` 这三项允许的标准基础，并核对最终证明项确实具有 `_root_.RiemannHypothesis` 类型。

本流程还安排 `leanchecker --fresh` 重放。它使用**同一个 Lean 内核**，不是独立实现的验证器；更强的独立检查尚未执行。上述运行步骤是否成功须看状态与原始日志。源码没有明显占位洞、导入图完整、抽查未见错误，都不能替代这些结果，更不能单独支持“黎曼猜想已经解决”的宣布。

静态检查确认的是文件结构、声明文本和已审阅片段的行为，未逐行完成这套数学论证的独立复核。没有发现决定性错误，应当按这个有限范围理解；未来若出现构建失败，也应区分资源不足、工具配置问题与具体证明错误。
