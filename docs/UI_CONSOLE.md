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

## Who does what

The console splits along one line: **an administrator manages customers, and a
customer runs their own line.**

| An administrator | A customer |
| --- | --- |
| Adds customers, assigns numbers, provisions devices | Chooses the extension an API call uses and where an unmatched number lands |
| Reveals extension credentials when handing over a device | Places and receives calls, reads voicemail and recordings |
| Edits a customer's call flows for them, one customer at a time | Owns extensions, ring groups and every call flow |
| Sees call history read-only | Switches recording on per device |

Nothing in the administrator's console dials. `POST /admin/api/calls` answers 403 for
an administrator, and an administrator cannot create the customer's API keys or
webhooks (`403`): those belong to the customer, and the administrator manages what
already exists.

Call flows are different: the administrator may edit a customer's flows, because a
customer's line is the administrator's job to keep answering. The builder stays
customer-scoped - `POST /admin/api/call-routes` resolves the owner from the *target*
(a number, extension or group), so naming another customer in the request cannot move
a flow across tenants, and writing under an unknown target is refused. In the console,
`canDesignFlows()` unlocks the palette, the save button, remove controls and dragging
only once a customer is chosen in the routing owner selector; without one, the page
says so rather than offering a dead control.

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

## Provisioning: assigning a number builds a working line

`POST /admin/api/numbers` accepts `inbound_extension: "auto"` (what the console sends
by default for a new number). The number is saved first, then `provision_number()`
builds everything else in one step:

| Created | Where it lives |
| --- | --- |
| A three-digit extension (`next_extension_number()`, 101 upwards) | `extensions`, owned by the customer |
| Its SIP credentials (username = the extension, generated password) | `extensions.sip_password_enc`, revealed on demand |
| The DID link, so inbound calls actually ring | `phone_numbers.inbound_extension` |
| The default caller ID when that extension has none | `phone_numbers.default_outbound` |
| A default flow for the number and one for the extension | `call_routes` and `routing_flows` |
| An activity entry and a notification | `activity_history`, `notifications` |

That default flow is the workflow the product promises, and `inbound_plan()` is the
one place it is turned into a call plan, so the engine and the builder cannot drift:

| Call arrives on | What rings | Nobody answers |
| --- | --- | --- |
| The customer's primary number | Every active device the customer owns, at once | The call ends |
| A number tied to one extension | Just that extension | The call ends |
| A number whose flow ends in voicemail | The ring step, then that mailbox | The caller leaves a message |

Adding a device extends the primary number (`sync_primary_flows`), because a main
line rings everyone - but only while that flow is still the generated one: a single
ring step, the default 25s timeout, no group and only the customer's own extensions.
The moment the customer designs something of their own, it is never rewritten.

Extension numbers are the primary key and therefore unique platform-wide, so
provisioning never hands a customer a number another customer already owns; the
suggestion comes from `GET /admin/api/extensions/next`. The customers' own
`POST /admin/api/extensions` behaves the same way: leave the password blank and the
server generates one, then writes the extension's default flow.

The SIP username is the identity the device authenticates with, so the platform owns
it: it *is* the extension number (`idx_extensions_sip_username`, a unique index on
`extensions.sip_username`). `save_extension()` derives it and ignores whatever a
caller sends, both console forms show it read-only, and a device account linked to an
extension inherits it too - an account may not take a username that is an extension
number, nor one another account already uses. Passwords stay the customer's to set;
the two default flows stay editable in the builder.

`GET /admin/api/extensions/<extension>/credentials` (owner or administrator) returns
the effective credential: if a device account is linked to the extension, that account
is what Asterisk ends up using, so the endpoint reports it as the source.

## Call flows: one builder, three kinds of target

A flow can belong to a number, an extension or a group, and the builder treats them
identically (`#route-target` groups the options by kind):

* **Numbers** — stored in `call_routes`, keyed by the number, as they always were.
* **Extensions** and **groups** — stored in `routing_flows`, keyed by
  `(owner_user_id, target_type, target)`. They live in their own table because
  `call_routes.phone_number` carries a legacy `UNIQUE` constraint that a second kind of
  target cannot satisfy, and rebuilding a table that holds customer data is not worth
  the risk.

A **group** (`extension_groups`) is a named set of the customer's extensions with a
ring timeout. A group can be the target of its own flow, and it can be chosen as the
ring destination inside any flow: the step then carries `group_id` alongside the
resolved `extensions`, and validation rejects a step whose destinations are not the
group's members. Deleting a group removes its flow; deleting an extension removes its
flow and takes it out of every group.

`POST /admin/api/call-routes` takes `target_type` (`number`, `extension`, `group`) and
dispatches to the right store call. Customers can only target what they own; an
administrator's request resolves the owner from the target itself (falling back to the
customer named in `owner_user_id`), so a flow cannot be written across customers.

A fresh ring step arrives pre-filled with what that target rings by default
(`flowStepDefaults()`): every device of the customer for a number, that device for an
extension, the members for a group. The main line therefore keeps the "rings everyone"
behaviour as the customer adds phones, and removing one is easier than finding it.

> **What rings today.** Inbound calls are answered by the ARI worker through
> `inbound_plan()`, the same planner the builder validates against: the customer's main
> line rings every active device they own, a number tied to one extension rings just
> that extension, and a flow that ends in voicemail falls back to the mailbox. Answer
> connects the caller, no answer ends the call (or takes the message). A step the
> customer adds - hours, groups, a second ring stage - is stored and validated, and the
> primary/extension/voicemail shape above is executed today.

## Customer-first pages: numbers, devices, integrations

Three pages answer "whose?" before "what?":

* **Numbers** — `#number-picker` lists every customer with how many numbers they hold.
  Choosing one scopes `#number-list` to them; each row links to the flow that answers
  that number (`data-number-flow` opens the builder on it).
* **Devices & SIP** — `#sip-picker` lists extensions and devices per customer, and the
  extension-credential list follows the choice.
* **Integrations** — `#integration-picker` scopes API keys, webhooks and deliveries.
  The customer keeps the create buttons; the administrator sees the same records with
  manage actions only.

The choice lives in `numberOwner`/`sipOwner`/`integrationOwner`, not in the DOM, so a
refresh keeps the page where it was. `resolvePickedOwner()` keeps a valid selection and
otherwise falls back to the first customer that actually has rows, so a page never
opens on an empty customer by accident.

## Routing defaults belong to the customer

`settings.call_defaults` is a per-customer map (`{user_id: {outbound, fallback}}`):

* `GET /admin/api/call-defaults` — the customer reads their own; an administrator must
  name `?customer_id=`, otherwise `400` (unknown customer: `404`).
* `POST /admin/api/call-defaults` — the customer saves `{outbound, fallback}` from
  **Numbers → Call defaults**; an administrator gets `403` ("call defaults belong to
  the customer").
* Resolution order when nothing is chosen: the customer's stored defaults, then the
  legacy platform `default_extension`/`inbound_fallback_extension`, then their first
  active extension.

Recording is per device for the same reason: `extensions.recording_enabled` on each
extension decides, there is no global policy panel, and a platform-level
`recording_enabled` no longer vetoes a device that opted in.

## Live system board (administrator)

`#system-board` sits at the top of the administrator's Overview and is refreshed from
`GET /admin/api/system` every five seconds by `loadSystem()`:

| Tile | Source |
| --- | --- |
| Calls in progress | live calls with `ringing` and `connected` split out |
| Peak at once today | highest simultaneous count from today's call events |
| Calls today / answered | today's totals |
| Devices registered | PJSIP endpoints that are online, out of the total |
| Asterisk channels | active channels |
| Recordings running | calls currently in `recording` state, plus today's count |
| Host load | load average as a percentage of CPU count, and memory use |

The tiles are updated in place (`textContent`), so polling never repaints the page.

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
* `.venv/bin/python tools/livecheck.py` — logs into a running preview as the
  administrator and as a customer and asserts the promises above over HTTP: the system
  board answers, call defaults belong to the customer (administrator `400`/`403`), the
  administrator cannot create keys or webhooks but can edit a customer's flows and
  build their groups, a spoofed owner cannot move a flow, and recording is per device.
* `node tools/uicheck.js` — boots the real console in jsdom against captured fixtures
  and asserts the promises above: refreshes that paint nothing, only customers dial,
  customer-first pickers, the flow builder's administrator mode, the system board, the
  SIP username is fixed, provisioning reveals
  credentials.
* `node tools/contrastcheck.js` — audits every text colour in `admin.css` against its
  own skin's surfaces, so the light and dark themes both stay legible.
* `node tools/csscheck.js` — cross-checks `.class` names between `admin.css`,
  `admin.html` and `admin.js`: nothing styled without a user, nothing applied without
  a rule.
* `tools/devpreview.py` — a seeded preview (`PREVIEW_DATA`, default `/tmp/eip-preview`)
  for looking at both consoles with real data.
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
2. **A poll never repaints what did not change.** Three layers, strongest first:
   `loadState()` skips the repaint entirely when the payload is unchanged;
   `refreshDeviceStatus()` compares before touching the DOM; and every list is written
   through `paint(id, html)`, which compares the markup with what is already there and
   leaves the existing nodes in place when they match. Identical bytes written again
   would still swap every child node out, which reads as a flicker on both consoles,
   so the comparison is on the markup, not on the data. Any repaint that does happen
   runs under `body.updating`, which suppresses entrance animations. An idle console
   is visually static, and a changing one only moves what changed.

## Keyboard and motion

Every interactive element — buttons, nav items, tabs, rows, cards, journey
milestones, quick actions and form fields — has a `:focus-visible` ring.

Under `prefers-reduced-motion: reduce` all animation resolves instantly
(duration and delay are zeroed), looping ambience such as the workspace ring,
aurora and empty-state glyphs stops, sheen overlays are removed, and smooth
scrolling is disabled. Entrance animations are additionally switched off for
page cascades and list items, so nothing moves at all.
