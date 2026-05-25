# Dashboard Watchlist Finish — Design

**Goal:** Finish the watchlist redesign — turn `/watched` into a 4-column
kanban board, give `bid_placed` a real capture path (the place-bid modal), and
surface the per-lot action history on the lot-detail page.

**Status:** approved 2026-05-20. Single branch `dashboard-watchlist-finish`,
single PR, three review-gated tasks.

These are PRs 4–6 of the Direction A dashboard redesign (see memory
`dashboard-redesign-direction-a`). The 4-state `user_action` foundation (PR
#13) and the enrichment-data-quality work (PR #15) are already merged.

---

## Motivation

The four-state `user_action` migration shipped the data model — `UserAction`
is 4-valued, `auction_lots` carries `max_bid_cad` / `bid_placed_at` / `won_at`,
and every writer routes through `carbuyer.db.lot_state.apply_user_action`,
which appends an immutable `LotActionHistory` audit row on every transition.

But the UI never caught up. Three gaps remain, all flagged as follow-ups in the
four-state design doc (`2026-05-19-four-state-user-action-design.md`):

1. `/watched` renders a 4-bucket **vertical list**, not the kanban the
   redesign promised.
2. The "Bid placed" button posts `/lots/{id}/mark` with no `max_bid_cad`,
   which `apply_user_action` rejects — clicking it is a hard 422. There is no
   way to actually record a bid from the dashboard.
3. `LotActionHistory` is written on every transition but **never read** — no
   query helper exists and no page renders it.

## Constraints

- **Zero new JavaScript, zero new dependencies.** The dashboard is
  deliberately HTMX + Jinja + CSS with no build step. Every interaction below
  is achievable with HTMX attributes and CSS alone.
- **No schema change, no migration.** Everything needed already exists on
  `auction_lots` and `lot_action_history`.
- **Mobile-first.** Touch targets ≥44px; the kanban must work on a phone.

---

## Change 1 — Watchlist kanban (PR 4)

`/watched` becomes a 4-column board: Interested / Bid placed / Purchased /
Passed. Each column is the existing per-state bucket; the cards and their
Watch/Bid/Pass action buttons are unchanged.

### Layout

- A `#watchlist-board` wrapper holds the four `<section class="watched__column"
  data-state="...">` elements.
- **Desktop:** a 4-up CSS grid. Each column scrolls vertically and independently.
- **Mobile:** columns are ~85vw wide and laid out in a horizontally
  `scroll-snap`-ing row — the next column peeks past the viewport edge so it is
  discoverable. This reuses the `scroll-snap` pattern already used by the
  photo carousels.
- Each column keeps its heading + count badge (`{{ items | length }}`).

### Moving a card between columns

The Watch / Bid placed / Pass buttons already on every lot card remain the only
move mechanism (decision: "buttons only, no drag", 2026-05-20).

Today a `/mark` click swaps the card **in place** — the `action_buttons` macro
defaults `hx-target` to `#lot-{id}`. On a kanban that strands a transitioned
card in the wrong column. Fix:

- Lot cards rendered on the watchlist call `action_buttons` with
  `target="#watchlist-board"`.
- `/lots/{id}/mark` gains a branch: when `HX-Target` is `watchlist-board`, the
  response is the **whole board fragment** — all four columns re-queried, with
  fresh count badges — instead of a single card. The transitioned card
  reappears in its new column.

This is the third response shape `/mark` already chooses between by target
(`-desktop`/`-mobile` → buttons fragment; default → single card). The new
branch is additive; the existing two are untouched.

### Files

- **New:** `templates/partials/watchlist_board.html` — the `#watchlist-board`
  wrapper + 4 columns. Rendered by both `pages/watched.html` (included) and the
  `/mark` board-response path.
- **Modify:** `templates/pages/watched.html` — replace the inline 4-bucket
  markup with `{% include "partials/watchlist_board.html" %}`.
- **Modify:** `routers/watched.py` — extract the bucket-building query into a
  helper reusable by both the page route and the `/mark` board response (e.g.
  `build_watchlist_buckets(session) -> dict[str, list[dict]]`).
- **Modify:** `routers/actions.py` — add the `HX-Target == "watchlist-board"`
  branch returning `watchlist_board.html`.
- **Modify:** `templates/_macros.html` — the `action_buttons` macro is already
  parameterized on `target`; watchlist cards pass `#watchlist-board`. No macro
  signature change.
- **New CSS:** `static/css/components/watchlist.css` — the grid + scroll-snap
  rules. Imported into `tailwind.css`.

### Accepted trade-off

Every button click re-renders the entire board, which resets each column's
scroll position. At single-operator scale (tens of watched lots) this is
acceptable. Surgical per-card out-of-band swaps are **out of scope** — listed
as a deferred refinement, not built.

---

## Change 2 — Place-bid modal (PR 5)

The "Bid placed" button stops posting `/mark` directly and instead opens a
modal that captures `max_bid_cad`.

### The button

- Watch and Pass keep their current behaviour: `hx-post /lots/{id}/mark`
  toggles.
- **Bid placed** changes to `hx-get /lots/{id}/bid-modal` targeting a
  top-level `#modal-slot` container (added once to the base layout).
- Clicking "Bid placed" on a lot that is *already* `bid_placed` opens the same
  modal in **raise-max** mode (pre-filled with the current `max_bid_cad`). The
  bid button never toggles the state off — to leave `bid_placed`, the user
  clicks Watch or Pass.

Because the bid button now diverges from Watch/Pass, the `action_buttons` macro
renders it as a distinct case (a `hx-get` button, not a `hx-post` toggle).

### The modal route

- **New route:** `GET /lots/{lot_id}/bid-modal` → renders
  `templates/partials/bid_modal.html`. Lives in `routers/actions.py`,
  co-located with the `POST /mark` it feeds — all bid-flow code in one module.
- The route loads the lot, so raise-max pre-fill is read fresh at modal-open
  time. Consequently `action_buttons_fragment.html` does **not** need
  `max_bid_cad` threaded through it — this is simpler than the four-state
  design doc anticipated, and that doc's predicted "context-dict break point"
  does not occur.

### The modal itself — CSS-only, no JS

`bid_modal.html` is a `position: fixed` full-viewport overlay (backdrop +
centered panel). It is visible simply by existing in the DOM after the HTMX
swap — no `showModal()`, no `<dialog>`, no `hx-on` handler.

- **Form:** a single field, `max_bid_cad`, an `<input type="number">` with
  `required` and `min="1"` so the browser blocks empty/non-positive input
  client-side. The form posts `hx-post /lots/{id}/mark` with
  `action=bid_placed` and `max_bid_cad`, targeting the card or
  `#watchlist-board` as appropriate for the originating page.
- **Dismiss without submitting:** a Cancel button and the backdrop are
  `hx-get`s that swap empty content into `#modal-slot`, removing the modal.
- **Dismiss on success:** every successful `action=bid_placed` response from
  `/mark` appends an out-of-band element
  `<div id="modal-slot" hx-swap-oob="true"></div>` (empty). The card/board
  swaps as normal *and* the modal disappears in the same response.

Accepted cost of CSS-only vs `<dialog>`: no ESC-to-close, no focus trap.
Acceptable for a single-operator tool, and it preserves the zero-JS rule. The
server-side 422 in `actions.py` stays as a backstop for malformed input.

### Display

Wherever a lot object is in template scope (lot card, lot-detail decision
card), a `bid_placed` lot shows `max $X` next to its state pill, using the
existing `money` macro.

### Files

- **New:** `templates/partials/bid_modal.html`.
- **Modify:** `routers/actions.py` — add `GET /lots/{id}/bid-modal`, and append
  the OOB `#modal-slot` clear to successful `bid_placed` responses.
- **Modify:** `templates/_macros.html` — `action_buttons` renders the bid
  button as a `hx-get` modal trigger.
- **Modify:** the base layout template — add the empty `#modal-slot` container.
- **Modify:** `lot_card.html` / decision-card markup — show `max $X` on
  `bid_placed` lots.
- **New CSS:** modal overlay rules (in `watchlist.css` or a `components/modal.css`).

---

## Change 3 — Lot-detail action history (PR 6)

Render the `LotActionHistory` audit trail on the lot-detail page.

### Read helper

- **New function:** `lot_action_history(session, lot_id) -> Sequence[LotActionHistory]`
  added to `src/carbuyer/db/lot_state.py` — the natural home, since that module
  already owns the *writer* (`apply_user_action`) for the same table. Ordered
  `changed_at DESC` (newest first), using the existing
  `ix_lot_action_history_lot_id_changed_at` index.

### Rendering

- **Modify:** `routers/lots.py` `lot_detail` — also fetch history, pass
  `history` in the template context.
- **Modify:** `templates/pages/lot_detail.html` — a new "Activity"
  `<section>` at the end of the main column, after "Comparable sales".
- Each entry shows: the timestamp (`local_dt` filter), the state entered (as a
  state pill / label), `max $X` when the row is a `bid_placed` row, and a muted
  `source` (`dashboard` / `discord_bot`).
- A row with `user_action = NULL` (state cleared) renders as "Cleared".
- Empty history (lots predating the audit table): the section renders a quiet
  "No recorded activity" line. It is not omitted entirely — its absence would
  be ambiguous.
- **New CSS:** a simple vertical timeline, added to
  `static/css/components/lot-detail.css`.

---

## Testing

All tests use the existing `uv run pytest -q` harness and the `carbuyer_test`
database. TDD throughout — failing test first.

- **PR 4:**
  - `/watched` renders a `#watchlist-board` with 4 columns and correct count
    badges.
  - `watchlist_board.html` renders in isolation given a buckets dict.
  - A `POST /lots/{id}/mark` with header `HX-Target: watchlist-board` returns
    the board fragment with the lot in its new column and adjusted badge counts.
  - The existing `-desktop`/`-mobile`→fragment and default→card branches still
    return their original shapes (regression guard).
- **PR 5:**
  - `GET /lots/{id}/bid-modal` renders the form; in raise-max mode the
    `max_bid_cad` input is pre-filled with the lot's current value.
  - `POST /mark` with `action=bid_placed` + `max_bid_cad` succeeds, transitions
    the lot, and the response contains the OOB `#modal-slot` clear.
  - `POST /mark` with `action=bid_placed` and no `max_bid_cad` still returns
    422 (existing behaviour, regression guard).
  - The dismiss route swaps empty content into `#modal-slot`.
- **PR 6:**
  - `lot_action_history` returns rows newest-first.
  - `lot_detail` renders the Activity timeline with timestamp, state, bid
    amount, and source.
  - Empty-history lot renders the "No recorded activity" line.
- Full suite green; no regressions in existing dashboard view tests.

## Out of scope

- **Drag-and-drop** on the kanban — explicitly rejected (2026-05-20); buttons
  are the move mechanism.
- **Surgical OOB card moves** — the board re-renders wholesale; per-card OOB
  swaps are a deferred refinement.
- **Auctioneer bid-number auto-detect** — auto-transitioning to `bid_placed`
  from per-source bidder identity (see memory
  `auctioneer-bid-number-auto-detect`).
- The remaining Direction A redesign PRs — Auctions (grouped-by-event view),
  Garage, the Admin nav consolidation, SSE live updates, saved searches.
- Any schema change. None is needed.
