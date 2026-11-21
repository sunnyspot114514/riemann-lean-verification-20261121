import Lean

/-! A valid theorem and an observable successful end of elaboration. -/
theorem loaderGood : True := True.intro
#print axioms loaderGood
#eval IO.println "GOOD_PROOF_OK"

