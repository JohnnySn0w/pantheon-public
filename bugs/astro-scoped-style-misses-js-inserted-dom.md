---
symptom: "DOM inserted at runtime via JS innerHTML is unstyled — an Astro component scoped <style> does not apply to it"
fix: "use :global(...) for the runtime-inserted selectors, or <style is:inline>; scoped-style hashing only tags build-time markup"
domain: "astro"
---
## Symptom
In an Astro component, CSS in a `<style>` block styles the static markup fine, but elements created at runtime by client JS (`el.innerHTML = '<i class="livedot"></i>'`, `document.createElement`) render UNSTYLED — the scoped CSS for that class does not apply.

## Cause
Astro `<style>` is SCOPED by default: at build it rewrites both the selectors and the component markup to carry a per-component hash attribute (`data-astro-xxxx`), and the scoped rule only matches elements carrying it. DOM inserted by client JS at runtime never gets the build-time hash, so the scoped selector cannot match it.

## Fix
Globalize only the runtime-targeted selectors so they are not hashed:

```astro
<style>
  .livedot { /* static, scoped — fine */ }
  :global(.livedot) { /* also matches JS-inserted nodes */ }
</style>
```

Or make the block non-scoped with `<style is:inline>`. Keep scoping for everything else.

## Verification
personal-site-new (2026-09-20): JS-inserted `.livedot` activity dots rendered unstyled until the rule was made `:global(.livedot)`. Re-runnable: add `<style>.x{color:red}</style>` in an Astro component, insert `<span class="x">` via `innerHTML` in a client script — not red until the rule is `:global(.x)`.

## Discovered
- 2026-09-20, personal-site digital-world work (specialist)

## Keywords
astro, scoped-styles, :global, is:inline, innerHTML, runtime-dom, javascript, css, data-astro, unstyled, static-site
