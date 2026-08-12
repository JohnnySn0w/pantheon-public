---
symptom: "Pushes over a deploy key land on the branch but GitHub Pages keeps serving stale content — the internal pages-build-deployment workflow's last build timestamp predates the pushes (it only reruns on UI saves / user-authenticated events). Site looks frozen despite green pushes."
fix: "Ship the standard Actions deploy workflow (checkout + configure-pages + upload-pages-artifact + deploy-pages, on: push) and flip repo Settings -> Pages -> Source to 'GitHub Actions'. Normal on:push workflows DO fire for deploy-key pushes (empirically proven Rodeo-Org/main-site 2026-07-19: run event=push, actor=key creator, deploy green) — it is only the INTERNAL pages-build-deployment that ignores them. Diagnosis tell: compare `gh api repos/<o>/<r>/pages/builds/latest` created_at vs your push times."
domain: github, pages, deploy-key, actions, static-site
---

# GitHub Pages internal build does not trigger on deploy-key pushes

## Symptom
Deploy-key pushes succeed; `pages-build-deployment` last run predates them; the served site is stale. A POST to `/repos/{o}/{r}/pages/builds` can kick it manually but needs a PAT with Pages write on that repo (fine-grained PATs scoped to a personal account 403 on org repos).

## Cause
The internal Pages build ("deploy from branch") triggers on user-attributed events; deploy-key pushes don't qualify. Standard workflows with `on: push` DO trigger for deploy-key pushes.

## Fix
Commit `.github/workflows/pages.yml` (actions/checkout@v4 → actions/configure-pages@v5 → actions/upload-pages-artifact@v3 path:. → actions/deploy-pages@v4; permissions pages:write id-token:write; concurrency group pages) and set Pages Source = GitHub Actions (one UI click or `PUT /pages` with build_type=workflow by an owner).

## Discovered
- rodeo.baby first deploy, Rodeo-Org/main-site, 2026-07-19 (the operator noticed staleness; root-caused via pages/builds/latest timestamp vs push log).

## Keywords
gh pages stale, deploy key push no build, pages-build-deployment not running, github actions pages source, static site not updating
