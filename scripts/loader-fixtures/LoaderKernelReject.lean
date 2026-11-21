import Lean

/-! Exercise the synchronous kernel API with one valid and one invalid declaration. -/
open Lean Elab Command

run_cmd do
  let env := (← getEnv).toKernelEnv
  let good : Declaration := .thmDecl {
    name := `LoaderRegression.kernelGood
    levelParams := []
    type := mkConst ``True
    value := mkConst ``True.intro
  }
  let checked ← match env.addDeclCore 1000000 1000 good none with
    | .ok checked => pure checked
    | .error _ => throwError "VALID_KERNEL_DECL_REJECTED"
  let bad : Declaration := .thmDecl {
    name := `LoaderRegression.kernelBad
    levelParams := []
    type := mkConst ``False
    value := mkConst ``True.intro
  }
  match checked.addDeclCore 1000000 1000 bad none with
  | .ok _ => throwError "INVALID_DECL_ACCEPTED"
  | .error _ => logInfo "KERNEL_REJECTED_INVALID_DECL"

