# Vendored dependency: pyseccomp

- **Source:** https://github.com/cptpcrd/pyseccomp
- **PyPI:** https://pypi.org/project/pyseccomp/
- **Version pinned:** 0.1.2 (2021-01-02 release)
- **License:** MIT (Copyright (c) 2020 cptpcrd) -- see `LICENSE` in this
  directory, unmodified.
- **`pyseccomp.py` in this directory is byte-for-byte identical to the
  upstream wheel's `pyseccomp.py`** (verified via checksum at vendor time,
  T-3295). Do not hand-edit it -- any glue/workaround code belongs in
  `tools/germinate/planner_sandbox/launcher/seccomp/__init__.py` (the
  sibling directory, not this vendored copy), so future version bumps stay
  a clean diff against upstream.

## Why this package, not `libseccomp-dev` + the official cffi bindings

`pyseccomp` is a pure-Python `ctypes` wrapper -- "API compatible with
libseccomp's Python bindings, but ... available on PyPI and ... written in
pure Python" (upstream README). It needs only the already-present runtime
`libseccomp.so.2` (from the `libseccomp2` apt package), not the
`libseccomp-dev` headers or a C compiler. This host had `libseccomp2`
installed but not `libseccomp-dev` or `python3-seccomp` at T-3295 planning
time (2026-08-26); vendoring `pyseccomp` avoided a `sudo apt install` on
shared VPS infrastructure. See `launcher/seccomp/__init__.py` for the
runtime-path-pinning workaround this vendoring choice requires.
