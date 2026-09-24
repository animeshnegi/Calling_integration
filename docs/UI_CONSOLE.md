# Admin & customer console UI

The consoles are one static single-page app served by Flask; there is no server-side
rendering and no build step.

| File | Role |
| --- | --- |
| `web/admin.html` | Shell: sidebar, topbar, the 18 `.page` sections, the customer workspace overlay, the modal and the toast host. |
| `web/admin.css` | All styling. Theme tokens, glass surfaces, components, animations, responsive rules. |
| `web/admin.js` | All behaviour. Talks to the existing `/admin/api/*` JSON endpoints only. |
| `web/admin-login.html` | Standalone sign-in screen. Shares `admin.css`, so it is served from `/admin-assets/admin.css`. |

Flask only serves these files (`/admin`, `/login`, `/admin-assets/<file>`; 
`/admin/login` is kept as an alias for the sign-in route), so a change to the
interface cannot change the API, the database or the permission model.

Sign-in lives at **`/login`**. `login_required` redirects there, the sign-in screen
posts there, and the marketing page links there; nothing in the UI points at the old
`/admin/login` path.

## Two experiences, one shell

`GET /admin/api/state` returns `is_admin`, and the app sets a body class from it:

* `body.admin-theme` — role marker. Administrators land in the dark control room.
* `body.customer-theme` — role marker. Customers land in the light, business-facing view.

The role markers carry no styling. The palette is keyed on a separate pair of skin
classes, so either role can use either theme:

* `body.theme-dark` — dark glass control room (administrators' default).
* `body.theme-light` — light glass view (customers' default).

Both skins resolve the same custom properties (`--surface`, `--line`, `--text-*`,
`--acc-*`, `--shadow-*`) and declare their own `color-scheme`, so components are
written once and native controls (`<select>` options, scrollbars, date pickers)
follow the skin instead of the operating system. Role differences are then
declared in markup with `[data-admin-only]` / `[data-customer-only]`, which
`loadState()` toggles. Provider and carrier data is never part of a customer payload
in the first place — the API already strips `provider` from every number and returns
empty `providers`/`users` lists — and the UI additionally blocks those pages.

`body.theme-loading` hides the shell until `loadState()` resolves, so a signed-in
user never sees an unstyled flash of the wrong theme. Its backdrop follows the active
skin, and a short inline script applies the remembered skin before the first paint.

### Theme switch

A single control in the topbar switches the skin for either role. The choice is
remembered per role (`localStorage['eip-console-theme:<role>']`), so an administrator
who prefers light does not drag customers onto the dark palette in a shared browser,
and `localStorage['eip-console-role']` lets the pre-paint script restore the right skin
on a reload.

### Quiet polling

The console polls `GET /admin/api/state` every 15 s while the tab is visible, checks
`/admin/api/device-status` every 8 s and `/health` every 30 s. Polling must never look
like a refresh:

* `loadState()` hashes the payload (the CSRF token aside) and returns early when
  nothing changed, so an idle console performs **zero** DOM mutations per poll.
* When something did change, the repaint runs under `body.updating`, which
  neutralises the entrance animations, and the workspace keeps its scroll position.
* `body.updating` is cleared by the next navigation (`showPage()`), not right after
  the repaint: clearing it sooner would give the freshly rendered nodes their
  animations back and replay the entrance one frame later.
* `refreshDeviceStatus()` only touches the DOM when a registration status actually
  moved, so a device flapping shows up as its status pill changing rather than as the
  list rebuilding.

Entrances are therefore tied to actions, not to renders: `.page.entering` is added by
`showPage()` and removed 700 ms later, so the page cascade runs once per navigation,
while `quietly()`/`endQuiet()` decide whether a background repaint may animate at all.
Anything the operator drives — navigation, opening the customer workspace, switching
its tabs — calls `endQuiet()` first, so it keeps its motion.

## Customer workspace (administrator)

Administrators manage a customer from one place: `Open workspace` on the customers
page loads `GET /admin/api/customers/<id>` and renders ten tabs — Overview, Numbers,
Devices & SIP, Routing, Calls, Recordings, Requests, Billing, Integrations and
Activity — in a centred overlay, without a page navigation. Cards inside the Overview
tab ("needs attention") link straight to the tab that fixes the gap. The overlay fills
the viewport on small screens and `.ws-body` is the single scroll region inside it, so
long tabs scroll inside the panel while the page behind stays put. The standalone pages
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
| Page changes | `.page.entering > *` cascades its children with 26ms steps, capped at ~150ms; the marker lives for 700ms, so re-renders cannot replay it |
| Workspace | one `.ws-tab-ink` element glides between tabs, tab bodies cross-fade with `.swapping`, header chips and metrics stage in |
| Hover | transform-only lifts on cards, rows, metrics and quick actions, plus a one-shot glass sheen on large surfaces |
| Live state | status pills pulse when a value changes, badges and tab dots pulse when a count changes, the workspace device chip updates in place |
| Loading | `skeletonPanel` / `skeletonRows` placeholders and a `.panel.loading` progress hairline |
| Feedback | toasts carry a timer bar sized by `--toast-life` |

Two rules keep it feeling fast rather than busy:

1. **Cascades stay under ~0.2s** and only run on real navigation or re-render.
2. **Polls never re-animate.** `loadState()` skips the repaint entirely when the
   payload is unchanged, `refreshDeviceStatus()` compares state before touching the
   DOM, and any repaint that does happen runs under `body.updating`, which suppresses
   entrance animations. An idle console is visually static.

## Keyboard and motion

Every interactive element — buttons, nav items, tabs, rows, cards, journey
milestones, quick actions and form fields — has a `:focus-visible` ring.

Under `prefers-reduced-motion: reduce` all animation resolves instantly
(duration and delay are zeroed), looping ambience such as the workspace ring,
aurora and empty-state glyphs stops, sheen overlays are removed, and smooth
scrolling is disabled. Entrance animations are additionally switched off for
page cascades and list items, so nothing moves at all.
