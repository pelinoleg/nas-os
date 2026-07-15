# DESIGN.md — design contract for Notes

> This is the single source of truth for the visuals. Any UI is assembled **only** from
> the tokens below. Hardcoding hex, px paddings, radii, shadows — forbidden.
> Rule for Claude Code: if the needed token doesn't exist — don't make up a value,
> propose adding a new token.

---

## 1. Principles (constraints)

Stick to the boundaries — they're what makes the design "beautiful":

- **One surface.** Chrome (sidebar, toolbar) and editor content live in one
  theme. No pure-white `#fff` canvas inside a dark app.
- **One accent.** Exactly one accent color on screen. Everything else — neutrals.
- **Air.** Padding is generous and follows the scale. Cramped = cheap.
- **Hierarchy through typography, not color.** The difference between a heading
  and body text is size/weight, not rainbow colors.
- **Icons, not text.** Toolbar is icon-only (`quote`, `code`), no `66`/`CB` labels.
- **A clear empty state.** Empty = one placeholder, not 10 empty checkboxes.
- **Maximum:** 1 accent · 2 radii · 2 font weights · 2 shadow levels.

---

## 2. Color tokens

**Notes owns two palettes** — light = warm "paper", dark =
**Everforest** (warm gray-green walls). **Accent is green in both themes**
(emerald in light, sage in dark): this is the app's character, it does NOT inherit
the OS system accent. Only the shape (radius slider
`--r-sm/--r-lg`) and shadow scale are inherited from the system. Components use
**only** semantic names (`--surface`, `--text`, `--accent`).

```css
/* LIGHT (default, on .nt-root) — warm paper */
--bg: #F7F6F3;  --surface: #FFFFFF;  --surface-2: #F0EFEA;  --surface-inset: #ECEBE6;
--text: #1C1B1A;  --text-muted: #6B6A66;  --text-faint: #A3A29E;
--border: #E4E2DD;  --border-strong: #D2CFC8;
--accent: #1F9E6E;  --accent-hover: #178A5F;  --accent-soft: #E3F3EC;  --on-accent: #FFF;
--success: #1F9E6E;  --warning: #C98A1B;  --danger: #C0473B;
--callout-bg: #FBF3D9;  --callout-border: #F0E2AE;

/* DARK (.nt-root[data-theme="dark"]) — Everforest: warm gray-green walls */
--side-bg: #232a2e (sidebar, deepest);  --bg/--surface: #2d353b (main field);
--card-bg/--surface-2: #343f44 (cards, code);  --surface-3: #3d484d (popovers);
--text: #d3c6aa;  --text-muted: #9da9a0;  --text-faint: #859289;
--border: #414b50;  --border-strong: #4f585e;
--accent: #a7c080;  --accent-hover: #bacc8f;  --accent-soft: #425047;  --on-accent: #232a2e;
--success: #a7c080;  --warning/--star: #dbbc7f;  --danger: #e67e80;
/* content semantics (also present in light): */
--heading: #e4dbc4;  --link: #7fbbb3;  --code-fg: #e69875 (inline code ONLY);
--selection: #543a48;  --quote-border: #a7c080;  --veil: semi-transparent hover;

/* shape and spacing (shared) */
--radius-sm: var(--r-sm);  --radius-lg: var(--r-lg);   /* system slider */
--sp-1..--sp-7: 4 8 12 16 24 32 48px;
```

Modes ("Notes theme": follow panel / light / dark): auto follows the
panel theme, light/dark are forced. The window's top bar (`.win.nt-win .wbar`)
is colored from the same palette — window chrome follows the Notes theme. The native
bottom Markdown/WYSIWYG bar is removed: the switch is two icons in the toolbar.

Borders — only where unavoidable (floating popovers, tables,
empty checkbox outline). Columns (sidebar/list/editor) are separated by
TONE (`--side-bg` vs `--bg`), not lines. Cards, buttons, inputs, paper,
counters, tags — no borders: surface difference and shadow instead. Active card — background
`--accent-soft` + 2px left stripe `--accent` (inset box-shadow).

## 3. Typography

```css
:root {
  --font-ui:   ui-sans-serif, -apple-system, "Inter", system-ui, sans-serif;
  --font-mono: ui-monospace, "IBM Plex Mono", "JetBrains Mono", monospace;

  /* sizes — minimal scale */
  --fs-h1:   28px;  --lh-h1:   1.25;  --fw-h1:   700;
  --fs-h2:   22px;  --lh-h2:   1.3;   --fw-h2:   600;
  --fs-h3:   18px;  --lh-h3:   1.4;   --fw-h3:   600;
  --fs-body: 16px;  --lh-body: 1.6;   --fw-body: 400;
  --fs-meta: 13px;  --lh-meta: 1.4;   --fw-meta: 400;
}
```

Rules:
- Only 2 weights: `400` (text) and `600/700` (headings/accents). No stray `300`/`500`.
- `meta` (time, "saved 14:31", tags) — always `--text-muted`, size `--fs-meta`.
- Editor content width is NOT constrained (owner's decision, 2026-07-11);
  content padding on the scale is mandatory.

---

## 4. Component rules

**Editor surface**
- Background = `--surface`, text = `--text`. In dark this is dark, NOT white.
- Content inner padding: `--sp-6` horizontal, `--sp-5` top.

**Toolbar**
- Icon-only, size 20px, color `--text-muted`; hover → `--text` + background `--surface-2`.
- Active button → icon `--accent`, background `--accent-soft`, radius `--radius-sm`.
- Group separators — `1px` line `--border`, not arbitrary gaps.

**Todo / checkbox**
- Row = checkbox + text, gap `--sp-2`, row height matches `--lh-body`.
- Empty checkbox: `border 1.5px --border-strong`, background `--surface`, radius 6px.
- Checked: background `--accent`, checkmark `--on-accent`; text → `--text-muted` +
  `line-through`.
- **Never** render empty checkboxes with no text as a "placeholder". Empty →
  one placeholder line in `--text-faint`: "Add an item…".

**Code block**
- Background `--surface-inset`, `--font-mono`, `--fs-meta`..`--fs-body`, radius `--radius-lg`.
- Line numbers — `--text-faint`. Copy button in the corner, icon-only.

**Callout**
- Background `--callout-bg`, left border or outline `--callout-border`, radius `--radius-lg`,
  padding `--sp-4`.

**Note card in the list**
- Background `--surface`, hover → `--surface-2`, active → `--accent-soft` + text keeps
  contrast. Shadow only `--shadow-1`. Radius `--radius-lg`. Padding `--sp-4`.

---

## 5. Contract for Claude Code

When working on the Notes UI:

1. Take values **only** from the tokens above. Not a single raw hex/px in components.
2. Switch theme via `[data-theme]`, components are identical for light/dark.
3. Change **one layer per iteration**: `layout → color → typography → components`.
   Don't touch other layers without being asked.
4. Empty states, hover, focus, active, disabled — mandatory for every
   interactive element.
5. If a task needs a token that doesn't exist — don't make up a value. Propose a new token
   with justification, add it to this file.
6. Stick to the constraints: 1 accent, 2 radii, 2 font weights, 2 shadows, spacing on the scale.

---

## 6. Implementation in NAS-OS (implementation notes)

- Tokens are declared in `web/desktop.html` scoped to `.nt-root` (root element of the
  Notes window) — the contract applies to everything inside the window and to /notes (solo).
- Only `--r-sm/--r-lg` (radius slider) are inherited from the system — the rest of the
  palette values are local (see §2).
- Theme: `ntApplyTheme()` always sets `data-theme` (light|dark) on `.nt-root`
  AND mirrors it onto `.win` (class `nt-win`) — that's how the title bar gets colored.
- Rules for the Toast UI editor must be prefixed with `.nt-root .nt-ed …`:
  vendor CSS (`tui-editor-dark.css`) loads AFTER inline styles, and `.toastui-editor-dark …`
  selectors (0,2,0) override unprefixed rules.
- The Toast UI editor gets `theme:"dark"` in dark mode (needs the light icon
  sprite), everything else is colored via tokens.
- Floating popovers inside Notes (code block language list) — on `--surface-3`
  with a `--border` outline and `--shadow-2` shadow; hover states — via `--veil`.
- User data (titles, tags, paths, previews) is rendered ONLY via
  `textContent` / `.value` / percent-encoded data attributes: the i18n hook translates the
  whole innerHTML, including attributes, and dictionary phrases ("New note") would get mangled.
- Note text size: setting `SET.ntFont` (default = `--fs-body` 16px).
