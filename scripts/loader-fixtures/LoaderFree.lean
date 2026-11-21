import Lean

/-! Each callback returns only Unit, so imported objects cannot escape freeRegions. -/
open Lean

def loaderFreePhase (value : String) : IO Unit := do
  if let some file ← IO.getEnv "LEAN_LOADER_TEST_PHASE_FILE" then
    IO.FS.writeFile file value
  IO.sleep 120

unsafe def main : IO Unit := do
  initSearchPath (← findSysroot)
  for i in [:3] do
    loaderFreePhase s!"before:{i}"
    withImportModules #[{ module := `Lean }] {} fun env => do
      if (env.find? ``True.intro).isNone then
        throw <| IO.userError "FREE_REGIONS_MISSING_DECL"
      if env.header.regions.isEmpty then
        throw <| IO.userError "FREE_REGIONS_EMPTY_IMPORT"
      let mapped := (env.header.regions.filter (·.isMemoryMapped)).size
      IO.println s!"FREE_CYCLE_OK {i} imported={env.header.regions.size} mapped={mapped}"
      loaderFreePhase s!"loaded:{i}"
    loaderFreePhase s!"after:{i}"
  IO.println "FREE_REGIONS_OK"

