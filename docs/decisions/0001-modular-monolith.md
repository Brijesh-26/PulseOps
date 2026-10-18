# ADR 0001: Modular monolith with separate processes

**Status:** accepted

**Context.** The system needs an HTTP API, asynchronous processing and a publisher for the outbox. It is built and operated by one person.

**Decision.** Use one Python package (`pulseops`) with domain modules and explicit interfaces (`QueuePort`, `InvestigationProvider`, `ExecutionSandbox`). Run it as three processes, `api`, `relay` and `worker`, from one image.

**Consequences.**
- One deployable artefact, shared models and simple local development.
- Processes scale independently.
- Module boundaries are a convention, not a network boundary. They could be split later along the same interfaces.
