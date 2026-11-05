Added Unix event-supervisor API and clock-driven termination primitives; harness routing remains unchanged. Full RL12 supervision is not yet implemented.
Refuse process-group signals when the captured leader identity is missing or changed; explicitly gate the Unix backend. Reaped leaders with live descendants report survivors rather than risk signalling a recycled group.
