# ADR 0007: Append-only audit with triggers, privileges and a hash chain

**Status:** accepted

**Decision.** `audit_record` has three layers of protection:

- triggers that reject UPDATE, DELETE and TRUNCATE for every role;
- a runtime role without UPDATE or DELETE grants;
- a SHA-256 hash chain, with appends serialised by an advisory lock and verified by `verify_chain`.

**Consequences.**
- Tampering is blocked for the application and detectable for a superuser who disables the triggers.
- Audit writes are serialised.
- The chain is not anchored externally (see docs/security.md).
