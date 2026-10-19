# forkable

An unofficial Python client and CLI for [Forkable](https://forkable.com) workplace meals. You can:

- see your meals for the week
- browse each day's restaurants, with sold-out status
- see every customization option for an item
- add, replace or remove meals, and move them between locations
- toggle Order / Don't order, and set special instructions

It uses only the Python standard library (Python 3.9+). It works with any Forkable account and
was reverse engineered from the web app. API notes are in [docs/SCHEMA.md](docs/SCHEMA.md).

## CLI

First [install](#install) and [log in](#log-in).

```bash
forkable whoami                 # your locations (ids), default location per weekday, auto-order setting
forkable week                   # this week's meals (next week's on weekends)
forkable week 2026-11-02        # the week containing that date
forkable day 2026-11-05         # that day's meal(s) + every location's delivery (ids, lunch/dinner)
forkable menu 2026-11-05        # restaurants + items with ids, prices, ratings, capacity, SOLD OUT
forkable menu 2026-11-05 -v     # ...including descriptions and every customization option
forkable menu 2026-11-05 --club 1234        # a different location's menu
forkable item 2026-11-05 <menu_id> <item_id>    # one item's customization options
```

Setting a meal:

```bash
# Replace your meal that day (or add one if you have none). Options are matched by id or
# case-insensitive name; errors list the valid choices.
forkable order 2026-11-05 <menu_id> <item_id> --opt bread=multigrain --opt toasted=toasted --dry-run
forkable order 2026-11-05 <menu_id> <item_id> --opt "sides=brussel,sweet potato" --note "No onions, please"

forkable order 2026-11-05 <menu_id> <item_id> --club 1234   # switch to another location for that day
forkable order 2026-11-05 <menu_id> <item_id> --add         # add an extra meal instead of replacing

forkable note 2026-11-05 "No onions, please"   # change instructions ("" clears)
forkable dont-order 2026-11-05                 # Order / Don't order toggle (keeps the selection)
forkable confirm 2026-11-05
forkable skip 2026-11-05 --dry-run             # remove the meal entirely
```

Notes:

- If you have several meals on a day, `day` shows short ids. Pass one with `--piece <id>`.
- If a location has both lunch and dinner, use `--window dinner`. `--delivery <id>` picks an exact delivery.
- Put `--json` before the command for raw output: `forkable --json week`.
- `order` keeps the replaced meal's note unless you pass `--note`.

### Order vs. Don't order, and suggested meals

If **auto order is OFF** for you (`forkable whoami` shows it), Forkable may pre-fill a suggested
meal for some days. Those show as `[DON'T ORDER]`, and you only receive one if you confirm it
(`forkable confirm DATE`) or pick something else. Picking a meal yourself confirms it automatically.

### What `order` does

| Your meal that day | Action | Mutation |
|---|---|---|
| none (or `--add`) | add a meal | `addPiece` |
| at the same location | swap item / options | `replacePiece` |
| at a different location | move it there ("Replace meal above" in the web app) | `replaceAllPieces` |

## Install

With [uv](https://docs.astral.sh/uv/) (`brew install uv`):

```bash
uv tool install git+https://github.com/benfwalla/forkable.git   # puts `forkable` on your PATH
uv tool upgrade forkable-cli                                # later, to update
```

Or from a clone: `uv run forkable week` (no install step), or `pipx install .` / `pip install .`.
Without installing anything: `python3 -m forkable` from the repo directory.

## Log in

The CLI reuses your Forkable login. Log in at https://forkable.com/mc/ in your browser first,
however your company does it (Okta and other SSO are fine). Then run:

```bash
forkable login
```

On a Mac this finds your Forkable login in whichever browser you use and copies the session (the most
recently used one, if you're logged in in several). On Windows or Linux, copy the cookie by hand (below).
`--browser chrome|brave|edge|arc|chromium|firefox|safari` picks one, and `--profile "Work"` picks a profile.

| Browser | What happens |
|---|---|
| **Firefox** | Just works. Firefox doesn't encrypt cookies, so there's no prompt. |
| **Chrome, Brave, Edge, Arc** | One macOS Keychain prompt (explained below). |
| **Safari** | macOS blocks it unless your terminal has Full Disk Access. Copy the cookie by hand instead (below). |

For Chromium browsers, forkable explains the macOS prompt and asks you to confirm before anything is accessed:

> *"security wants to use your confidential information stored in 'Chrome Safe Storage' in your keychain."*

- That's the key Chrome uses to encrypt its saved cookies. forkable uses it once to decrypt **only** the
  forkable.com session cookie. Nothing else is read, kept, or sent anywhere.
- Enter your Mac login password and click **Allow**. Avoid **Always Allow**: it lets *any* program use
  that key without asking.
- On a work laptop, endpoint security software may flag this, because it's the same access cookie-stealing
  malware uses. If you'd rather avoid it, copy the cookie by hand instead (below).

The session is saved to `~/.config/forkable/session.json` (readable only by you) and lasts about 6 weeks.
**Logging out of Forkable in the browser ends it.** When a command says you're not logged in, run `forkable login` again.

### Other ways to log in

- **Copy the cookie by hand** (any browser or OS, no prompts): log in at https://forkable.com/mc/, open
  developer tools and find the cookies for `https://forkable.com`:
  - Chrome / Edge / Brave / Arc: `Cmd+Opt+I` (`F12`) → **Application** → **Cookies**
  - Firefox: `Cmd+Opt+I` → **Storage** → **Cookies**
  - Safari: **Settings** → **Advanced** → *Show features for web developers*, then **Develop** →
    **Show Web Inspector** → **Storage** → **Cookies**

  Copy the **Value** of `_easyorder_session`, run `forkable set-session` and paste it at the prompt. The
  input is hidden, so it stays out of your shell history.
- **Emailed code**: `forkable login --code you@example.com`. Only for accounts *without* SSO; SSO accounts
  don't get the email, and the CLI tells you so.
- **Password**: `forkable login --password you@example.com` (`--mfa CODE` for two-factor).
- **Env var**: `FORKABLE_SESSION`, or a `.env` file in the current directory. This takes priority over the saved session.
- `forkable logout` forgets the saved session.

## Postman

`postman/forkable.postman_collection.json` covers what the client does: your account, deliveries
(your week), menus with options and sold-out data, and the ordering mutations. To use it:

1. In Postman, **Import** the file.
2. Copy your `_easyorder_session` cookie as described in [Other ways to log in](#other-ways-to-log-in)
   (or, after `forkable login`, take it from `~/.config/forkable/session.json`).
3. Paste it into the collection's **Variables** → `session` → *Current value*, and save.
4. Run **Deliveries / My deliveries**, then **Menus / Menus**. They fill in `deliveryId`, `menuIds`,
   `menuId`, `itemId`, `pieceId` and so on for the other requests.

A collection pre-request script fetches the CSRF token that mutations need. Requests in **Ordering**
change your real order. The file holds no credentials. Keep your session in *Current value*, not
*Initial value*, so it isn't shared if you export or sync the collection.

The collection is generated from `forkable/queries.py`. After changing queries, regenerate it with
`uv run python tools/build_postman.py`.
