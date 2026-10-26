"""ORG-080 germination engine skeleton (T-5074, tranche GB).

CI-only, zero privilege: the only host backend is ``backend.SimulatedHostBackend``.
The component adoption seam a real component (T-5081 onward) adopts through an
adapter is: a contract YAML (``contract.py``), a sibling planner module whose
``plan_<leg>(request) -> list[dict]`` returns typed operation data
(``planner.py``), and an ``backend.EffectHandler`` for any new op kind.
"""
