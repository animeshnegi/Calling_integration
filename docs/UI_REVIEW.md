# Admin/customer UI review

The admin and customer use the same console shell with server-enforced access
rules and role-specific navigation. This pass changes presentation, not SIP,
recording, routing execution, authentication or billing behavior.

## Reviewed layout contracts

- Closed dialogs, drawer and scrim are invisible and do not intercept clicks.
- Mobile navigation is off-canvas and opens above its scrim; desktop navigation
  stays fixed. Tables scroll within their containers instead of widening pages.
- Customer drawer has a fixed identity header, one independent scroll region and
  sticky resource tabs. Modal headers/footers remain outside their scroll area.
- `[hidden]` always wins over component display rules for role-specific controls.
- Documentation typography/grid rules are scoped, not global overrides.
- Statistic cards are cards, not inline badges. System metrics, chart bars and
  key/value rows have explicit layouts. Dark body foreground is explicitly set.
- Form/select grids can shrink on mobile, including number filters and routing.
- Navigation icons are bundled SVGs, not font-dependent decorative glyphs.
- Login spacing, control sizing, panel hierarchy, tables and metric cards follow
  the same Tailwind-based spacing/color system. No Tailwind CDN is used.

## Repeatable browser verification (development fixture server only)

```bash
PORT=5001 .venv/bin/python tools/devpreview.py
# In another terminal. These are development-only tools, not runtime dependencies:
npm install --no-save --package-lock=false playwright-core @sparticuz/chromium
UI_BASE_URL=http://127.0.0.1:5001 node tools/formal-ui-browsercheck.js
```

Linux must have Chromium's NSS/NSPR runtime libraries, or set `LD_LIBRARY_PATH`
to the bundled Chromium runtime libraries in a minimal sandbox. This harness is
for the seeded local preview accounts, not a live server. It does not register
SIP devices, place calls, save settings, or modify customer records. Repeated
runs may hit login throttling: restart the **development preview** to reset it;
do not disable production login limits.

The harness checks both roles, every visible navigation page at desktop/mobile
widths, role visibility, modal/drawer state, independent scroll, theme foreground,
mobile sidebar behavior and horizontal overflow. `UI_SCREENSHOTS=/absolute/path`
optionally saves dashboard screenshots outside the repository.

This is not an end-to-end telephony acceptance test. A live PBX, MySQL, nginx,
public TLS, SIP registration, inbound/outbound calls and audio playback still
require separate deployment validation. Legacy jsdom/CSS harnesses include
older routing/branding assumptions and are not claimed as passing this review.
