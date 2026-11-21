import LoaderArtifact

/-! The same module name is used for valid and deliberately corrupted copies. -/
example : LoaderArtifact.value = 7 := rfl
#eval IO.println "ARTIFACT_IMPORT_OK"

