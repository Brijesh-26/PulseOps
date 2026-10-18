# ADR 0006: The model proposes, the backend decides

**Status:** accepted

**Decision.** Model output is advisory data. It must pass a strict versioned schema and backend validation:

- citations must reference existing evidence;
- impact is replaced with graph-computed impact;
- the backend runs its own injection detection;
- the action must be allowlisted, in scope and above the confidence threshold.

A human with the `approver` role, who is not the proposer, must approve. Preconditions are checked again at execution, and execution happens only in a sandbox behind an interface.

**Consequences.**
- The model can be wrong without causing unsafe actions.
- Investigations that the validator weakens are recorded with their validation issues rather than hidden.
