EdSider is Sri Lanka's exam-practice platform (Grades 6-13, Sinhala, Tamil and English). This system carries the brand of www.edsider.com - EdSider purple, Poppins headings, Inter body, soft rounded cards on a warm-to-lavender ground - into EdSider AI, the review tools where teachers and data-entry staff find repeated questions in the bank. One idea governs every review screen: **the system suggests; a person decides.** Make it easy to read two questions, see why the system thinks they match, and record a verdict, without ever implying the tool decided.

Three apps share it: the data-entry tool (seven pages), the teacher review app (one page), and the paper-assignment app (one page).

## Content fundamentals

- **Warm and plain on the brand's surfaces, exact inside the tools.** The site speaks to students ("Study Better. Practice Smarter. Achieve More.", "Learn in Your Preferred Language"). The review tools speak to colleagues: say what happened and why - "Safety checks fired - score lowered from 98% to 62%", "3 question(s) still not marked reviewed."
- **Sentence case** for titles, buttons, labels and verdict pills ("Find duplicate questions", "Likely duplicate"). The site's marketing CTAs use Title Case ("Start Your Journey"); keep that to marketing.
- **"You"** for the reader in the teacher app ("No papers have been assigned to you yet."); neutral in the data-entry tool.
- **Numbers:** similarity as a whole percent ("97% similar"); counts with separators ("16,084"); ids with a hash ("#58317", "Assessment #1127"). The site writes totals as "1000+", "50K+" - fine on marketing stats, never in the tools, where counts are exact.
- **Languages by their own names in their own script:** සිංහල, தமிழ், English.
- **Separators:** a spaced hyphen in metadata ("Grade 10 - Mathematics - Mensuration").
- **No emoji, anywhere.**
- **Never imply automatic action.** No "Merge", "Auto-fix", "Delete duplicate". The verbs are Confirm duplicate, Not a duplicate, Skip, Undo.

## Visual foundations

**Colour.** `primary` (#6A1B9A, EdSider purple) is the brand: primary buttons, the active nav pill, the accent word of a page title, links, focus rings - and the guard layer, because a safety check firing is the system working, not a problem. `primary-dark` is its hover. `text-dark` (a purple-black, #1B1533) for headings and body, `text-muted` for everything secondary. Pages sit on `surface-page` (a faint lavender) with white `surface` cards bordered in `border-soft`; `surface-sunk` (the brand lavender) marks decision bars and stems.

The brand's accents - `accent-coral`, `accent-gold`, `accent-blue`, `success` - each with a `-soft` tint, fill icon tiles and language pills exactly as on the site. They are bright and read below 4.5:1 as small text; use them for icons, tiles and pills, never for body text.

Similarity has its own signals, built on those same hues but deepened to read as text: `dup` (coral), `review` (gold), `distinct` (blue), each on its `-soft` fill. Distinct is blue so duplicate and distinct never depend on red versus green. `ok` (the brand green, deepened) is for success notices only. Every signal always carries its word.

**Gradients.** `gradient-hero` (peach, cream, lavender, 135deg) is the brand's signature background. Use it once per page, behind the `PageHeader`; never behind dense content. `gradient-soft` for a section background. Gradients are light-theme only - in dark the band is flat `surface-sunk`.

**Type.** Poppins (`heading`) for everything that is a heading, a figure or a pill: `display` 33.6px extrabold page titles, `title` 22px, `heading` 17px, `stat` 25.6px extrabold figures and similarity scores, `pill` 13.6px bold. Inter (`body`) for reading: `body-lg` 16.8px with a 1.75 line height for question and answer text and subtitles, `body` 16px, `button` 16px semibold, `small` 14px medium for metadata. `hero` (49.6px) is marketing-only. Sinhala and Tamil fall back to Noto Sans Sinhala / Tamil in both roles; set `lang` on question text. Ids use Inter with tabular figures.

**Spacing.** From the site's own layout: `space-2` 8 between pills and buttons, `space-3` 14 icon-to-text, `space-4` 20 between cards, `space-card` 22 inside cards, `space-6` 30 button padding, `space-8` 44 between sections.

**Shape.** Generously rounded: `radius-card` and `radius-btn` 16px, `radius-icon` 14px for icon tiles, `radius-md` 12px for notices, inputs and option tiles, `radius-pill` for nav items, pills, toggles and the section rail.

**Elevation.** Soft, purple-black shadows rather than hard borders: `shadow-soft` for cards in lists, `shadow-card` for stat cards, `shadow-card-hover` with a 4px rise. Primary buttons carry a purple glow (`shadow-primary`) and rise 2px on hover.

**States.** Hover: primary buttons go `primary-dark` and lift; secondary buttons, nav links and rail buttons turn `primary` on `primary-soft`. Focus: a solid 3px `primary` outline, 2px offset. Disabled: 45% opacity with no lift, always beside a notice saying what enables it. Reduced motion removes the lifts.

**Controls.** The site's outline-button border (`border-outline`, #DDD5F0) is too faint to mark a control (1.3:1); in the tools, inputs, checkboxes, toggles and secondary buttons use `border-strong`.

**Layout.** A white `TopNav` across the top; a `PageHeader` band; content in a column up to 1200px. Pair reviews put the two questions side by side and stack below 720px. Long pages (Paper intake) get a `SectionRail` floating at the left edge. Nothing scrolls sideways at phone width.

## Iconography

The site draws its own inline outline icons: 24px grid, ~1.8px stroke, round caps and joins, `currentColor`. In the rail they are `text-muted`, turning `primary` in a `primary-soft` circle when active; in stat and feature cards they sit in a 48px `radius-icon` tile with an accent colour and its soft fill. The icons in this bundle (`search`, `copy`, `paper`, `check`, `upload`, `bank`, `users`, `plus`) are drawn in that style as stand-ins - they are not copied from the site. Always pair an icon with a text label or an `aria-label`.

## Logo

`assets/Logos/edsider-ai-logo.png` is the **main logo** of the review tools: the purple mortarboard with the blue arrow, the black EdSider wordmark with a purple dot on the i, and "AI" in a purple-to-blue gradient. Put it at the left of every `TopNav` and phone header, on a white or `surface-page` ground. Its file has wide transparent padding: set it 50px tall in the 76px nav bar (40px on a phone) so the artwork reads at about 34px. Keep clear space equal to the dot's height on every side; do not recolour, stretch, crop off the "AI" or place it on the gradient's darker end.

`assets/Logos/edsider-logo.png` is the plain EdSider lockup, for student-facing and marketing surfaces. Never set "AI" beside it by hand - use the AI lockup.

## Using it

- Load `tokens.css` and `components/bundle.css` (it imports Poppins, Inter and Noto Sans Sinhala/Tamil from Google Fonts), then React 18 and `components/bundle.js`; components are on `window.EdSider`.
- Build review screens from `PairReview`; intake from `DraftCard`; use `Notice` wherever the Streamlit code used `st.info` / `st.success` / `st.warning` / `st.error`.
- `Score` ticks must match the threshold that page really uses: 0.85 / 0.92 for a single-question check, 0.95 for the whole-bank, within-paper and cross-passage sweeps.
