import Init

/-! A seed artifact produced by the original toolchain for compatibility tests. -/
namespace LoaderArtifact
def value : Nat := 7
theorem certificate : value = 7 := rfl
end LoaderArtifact

