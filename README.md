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
# Account
forkable login                                # reuse your browser's Forkable login (see Log in)
forkable set-session                          # or paste the session cookie
forkable whoami                               # locations, default per weekday, auto-order
forkable logout

# Browse
forkable week [<date>]                        # the week's meals (next week on weekends)
forkable day <date>                           # that day's meals + every location's delivery
forkable menu <date> [-v]                     # items, prices, SOLD OUT (-v: descriptions, options)
forkable item <date> <menu_id> <item_id>      # one item's customization options

# Order: replaces your meal that day, or adds one if you have none
forkable order <date> <menu_id> <item_id> --opt bread=multigrain --opt "sides=brussel,sweet potato"
forkable order <date> <menu_id> <item_id> --note "No onions, please"
forkable order <date> <menu_id> <item_id> --add     # extra meal instead of replacing
forkable order <date> <menu_id> <item_id> -n        # dry run: show the plan, change nothing

# Adjust a day
forkable note <date> "No onions, please"      # "" clears
forkable dont-order <date>                    # keep the meal, but don't order it (alias: unconfirm)
forkable confirm <date>                       # order it
forkable skip <date> [-n]                     # remove the meal
```

Dates are `YYYY-MM-DD`. Shared flags:

| Flag | Commands | Use |
|---|---|---|
| `--club <id>` | `menu`, `item`, `order` | Another location (ids from `whoami`). With `order`, moves your meal there. |
| `--window <name>` | `menu`, `item`, `order` | `lunch` or `dinner`, when a location has both. |
| `--delivery <id>` | `menu`, `item`, `order` | An exact delivery (ids from `day`). |
| `--piece <id>` | `order`, `note`, `confirm`, `dont-order`, `skip` | Which meal, if you have several that day (short ids from `day`). |
| `--json` | all (before the command) | Raw JSON: `forkable --json week`. |

Notes:

- `--opt <modifier>=<option>` matches by id or by name (case-insensitive); errors list the valid choices.
  Separate several options with commas.
- `order` keeps the replaced meal's note unless you pass `--note`.
- `forkable <command> --help` lists every option.

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
`--browser chrome|brave|edge|arc|chromium|firefox|safari` picks one, and `--profile "Work"` picks a profile;
`-y` skips the confirmation.

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
