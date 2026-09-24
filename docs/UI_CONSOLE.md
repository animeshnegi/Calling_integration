# Admin & customer console UI

The consoles are one static single-page app served by Flask; there is no server-side
rendering and no build step.

| File | Role |
| --- | --- |
| `web/admin.html` | Shell: sidebar, topbar, the 19 `.page` sections, the customer workspace drawer, the modal and the toast host. |
| `web/admin.css` | All styling. Theme tokens, glass surfaces, components, animations, responsive rules. |
| `web/admin.js` | All behaviour. Talks to the existing `/admin/api/*` JSON endpoints only. |
| `web/admin-login.html` | Standalone sign-in screen. Shares `admin.css`, so it is served from `/admin-assets/admin.css`. |

Flask only serves these files (`/admin`, `/admin/login`, `/admin-assets/<file>`), so a
change to the interface cannot change the API, the database or the permission model.

## Two experiences, one shell

`GET /admin/api/state` returns `is_admin`, and the app sets a body class from it:

* `body.admin-theme` — dark control room for administrators.
* `body.customer-theme` — light, business-facing view for customers.

Both themes resolve the same custom properties (`--surface`, `--line`, `--text-*`,
`--acc-*`, `--shadow-*`), so components are written once. Role differences are then
declared in markup with `[data-admin-only]` / `[data-customer-only]`, which
`loadState()` toggles. Provider and carrier data is never part of a customer payload
in the first place — the API already strips `provider` from every number and returns
empty `providers`/`users` lists — and the UI additionally blocks those pages.

`body.theme-loading` hides the shell until `loadState()` resolves, so a signed-in
user never sees an unstyled flash of the wrong theme.

## Customer workspace (administrator)

Administrators manage a customer from one place: `Open workspace` on the customers
page loads `GET /admin/api/customers/<id>` and renders ten tabs — Overview, Numbers,
Devices & SIP, Routing, Calls, Recordings, Requests, Billing, Integrations and
Activity — in a side drawer, without a page navigation. Cards inside the Overview tab
("needs attention") link straight to the tab that fixes the gap. The standalone pages
remain available for cross-customer work.

## Adding a page

1. Add an entry to `pageMeta` in `web/admin.js` (`title`, `subtitle`).
2. Add `<section class="page" id="page-<key>">` to `web/admin.html`.
3. Add a nav button with `data-page="<key>"` (plus `data-admin-only` if privileged).

`showPage()` and `loadState()` do the rest. The page key is also the URL hash, so
pages are linkable.

## Verifying a change

* `PYTHONPATH=. .venv/bin/python -m pytest -q` — the API/settings suites (the console
  shares those endpoints, so this must stay green).
* Render both roles headlessly against captured `/admin/api/*` fixtures to catch
  runtime errors and missing DOM nodes without a browser.
* Parse `admin.css` and cross-check every class against `admin.html` and `admin.js`:
  a class applied in markup but absent from the stylesheet renders unstyled, and a
  class in the stylesheet that nothing applies is dead weight. Status values come from
  the API, so they are checked against captured fixtures instead of guessed.

## Motion and interaction

All animation lives in `admin.css` and follows one vocabulary, so timings stay
consistent and nothing outruns the interface:

| Concern | Rule |
| --- | --- |
| Entrances | `fadeUp` / `popIn` / `rowIn`; lists stagger through a `--i` index that `markStagger()` writes onto rows, cards and rows of grouped lists |
| Page changes | `.page.active > *` cascades its children with 26ms steps, capped at ~150ms |
| Workspace | one `.ws-tab-ink` element glides between tabs, tab bodies cross-fade with `.swapping`, header chips and metrics stage in |
| Hover | transform-only lifts on cards, rows, metrics and quick actions, plus a one-shot glass sheen on large surfaces |
| Live state | status pills pulse when a value changes, badges and tab dots pulse when a count changes, the workspace device chip updates in place |
| Loading | `skeletonPanel` / `skeletonRows` placeholders and a `.panel.loading` progress hairline |
| Feedback | toasts carry a timer bar sized by `--toast-life` |

Two rules keep it feeling fast rather than busy:

1. **Cascades stay under ~0.2s** and only run on real navigation or re-render.
2. **Polls never re-animate.** `refreshDeviceStatus()` compares state before
   touching the DOM, so an unchanged 8-second poll is a no-op.

## Keyboard and motion

Every interactive element — buttons, nav items, tabs, rows, cards, journey
milestones, quick actions and form fields — has a `:focus-visible` ring.

Under `prefers-reduced-motion: reduce` all animation resolves instantly
(duration and delay are zeroed), looping ambience such as the workspace ring,
aurora and empty-state glyphs stops, sheen overlays are removed, and smooth
scrolling is disabled. Entrance animations are additionally switched off for
page cascades and list items, so nothing moves at all.
