# health_collector fixtures

Verbatim `busctl --user --json=short ... GetAll` stdout from the real PersonaController1 emitter, run on a private scratch bus with pinned stub values (T-5410).

- `persona_getall_flag_off.json`: the fifteen base properties (the original nine plus the six ORG-106 `SeatState*` properties, T-5527).
- `persona_getall_flag_on.json`: the fifteen base properties plus the seven T-5137 health-leg properties (twenty-two) (`PANTHEON_PERSONA_HEALTH_LEG=on`).

Regenerate with `python3 -m tools.test_persona_controller_health_dbus_e2e --capture`. A test fails if these files differ from a fresh capture, or from the emitter's declared property names and signatures. Don't hand-edit them.
