"""Command line interface: `forkable <command>` (or `python3 -m forkable <command>`)."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import getpass
import json
import sys
from typing import Dict, List, Optional

from . import client as C
from .api import Day, Forkable, Menu, MenuItem, Selection, _squash
from .browser import BROWSERS
from .client import ForkableError, GraphQLClient, NotLoggedIn, SsoRequired

DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

MANUAL_COOKIE = """To copy your session by hand: log in at https://forkable.com/mc/ (Okta/SSO is fine), open
the browser's developer tools and find the forkable.com cookies:
  Chrome/Edge/Brave/Arc: Cmd+Opt+I > Application > Cookies
  Firefox: Cmd+Opt+I > Storage > Cookies
  Safari: Settings > Advanced > "Show features for web developers", then Develop > Show Web
          Inspector > Storage > Cookies
Copy the value of `_easyorder_session`, then run: forkable set-session"""

SSO_FALLBACK = f"""On macOS, `forkable login` (no flags) imports your browser's Forkable login.
{MANUAL_COOKIE}"""

KEYCHAIN_NOTICE = """Found your Forkable login in {app}. To read it, macOS will ask:
"security wants to use your confidential information stored in '{item}' in your keychain."
  * '{item}' is the key {app} uses to encrypt its saved cookies. forkable uses it once to
    decrypt only the forkable.com session cookie. Nothing else is read, kept, or sent anywhere.
  * Enter your Mac login password and click "Allow". Avoid "Always Allow": it lets any
    program use that key without asking.
  * On a work laptop, security software may flag this access. If you'd rather avoid it,
    answer "n" and use `forkable set-session` (copy the cookie by hand) instead."""

SAFARI_BLOCKED = """Safari: macOS only lets apps with Full Disk Access read Safari's cookies. Either copy the
cookie by hand (below), or give your terminal app Full Disk Access (System Settings > Privacy &
Security > Full Disk Access) and run `forkable login` again. Full Disk Access lets the terminal
read all your files, so on a work laptop copying the cookie is the better choice."""


def _money(x) -> str:
    return f"${x:.2f}" if x else ""


def _sel_line(s: Selection, show_id: bool = False) -> str:
    opts = "; ".join(f"{o['label']}: {_squash(o['value'])}" for o in s.options)
    status = "ORDER" if s.confirmed else "DON'T ORDER"
    pid = f" ({s.short_id})" if show_id else ""
    line = f"{s.name} — {s.menu_name} ({_money(s.price)}) [{status}]{pid}"
    if opts:
        line += f"\n      {opts}"
    if s.instructions:
        line += f"\n      note: {s.instructions}"
    return line


def _print_day(day: Day) -> None:
    label = day.date.strftime("%a %b %d")
    if not day.deliveries:
        print(f"{label}: no deliveries")
        return
    sels = day.my_selections
    if not sels:
        print(f"{label}: (no meal selected)")
        return
    multi = len(sels) > 1
    for s in sels:
        loc = "" if s.club_id == day.preferred_club_id else f"  [at {s.club_name}]"
        print(f"{label}: {_sel_line(s, show_id=multi)}{loc}")


def _print_item(item: MenuItem, verbose: bool) -> None:
    flag = "" if item.available else "  [SOLD OUT]"
    rating = f" ★{item.average_rating:.1f}" if item.average_rating else ""
    print(f"    {item.id:>5}  {item.name}  {_money(item.price)}{rating}{flag}")
    if verbose:
        if item.description:
            print(f"           {item.description}")
        for m in item.modifiers:
            _print_modifier(m, indent=11)


def _print_modifier(m, indent: int) -> None:
    pad = " " * indent
    req = "required" if m.min > 0 else "optional"
    pick = f"pick {m.min}" if m.min == m.max else f"pick {m.min}-{m.max}"
    print(f"{pad}[{m.id}] {m.name} ({req}, {pick}){' (hidden)' if m.hidden else ''}")
    for o in m.options:
        print(f"{pad}    {o.id:>5}  {o.name}{'  +' + _money(o.price) if o.price else ''}")


def _print_menu(menu: Menu, verbose: bool) -> None:
    cap = f"{menu.usage}/{menu.capacity}" if menu.capacity else "n/a"
    flag = "  ** SOLD OUT **" if menu.sold_out else ""
    print(f"\n[{menu.id}] {menu.name}  (capacity {cap}){flag}")
    section = None
    for it in menu.items:
        if it.section != section:
            section = it.section
            print(f"  -- {section}")
        _print_item(it, verbose)


def _parse_opts(pairs: List[str]) -> Dict[str, List[str]]:
    """--opt "bread=multigrain" --opt "26=22" --opt "sides=brussel,sweet potato" """
    out: Dict[str, List[str]] = {}
    for p in pairs or []:
        if "=" not in p:
            raise ForkableError(f"--opt expects MODIFIER=OPTION, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = [x.strip() for x in v.split(",") if x.strip()]
    return out


def _json_default(o):
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    return str(o)


def _add_where(s) -> None:
    s.add_argument("--club", type=int, help="location (club) id; see `forkable whoami`")
    s.add_argument("--window", help='service window, e.g. "lunch" or "dinner"')
    s.add_argument("--delivery", type=int, help="exact delivery id (see `forkable day`)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forkable", description="Unofficial Forkable meal CLI")
    p.add_argument("--json", action="store_true", help="print raw JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="log in (default on macOS: reuse your browser's Forkable login)")
    s.add_argument("email", nargs="?", help="your Forkable email (browser import: only accept this account)")
    how = s.add_mutually_exclusive_group()
    how.add_argument("--browser", "--from-browser", dest="browser", choices=BROWSERS,
                     help="browser to import the session from (macOS; default: check every installed browser)")
    how.add_argument("--from-chrome", dest="browser", action="store_const", const="chrome", help=argparse.SUPPRESS)
    how.add_argument("--code", action="store_true", help="log in with a 6-digit code Forkable emails you (non-SSO)")
    how.add_argument("--password", action="store_true", help="log in with a Forkable password")
    s.add_argument("--mfa", help="two-factor code (password login)")
    s.add_argument("--profile", help="browser profile name or directory (default: try all, most recent first)")
    s.add_argument("-y", "--yes", action="store_true", help="skip the Keychain explanation prompt")
    s = sub.add_parser("set-session", help="save a session cookie copied from your browser (SSO fallback)")
    s.add_argument("cookie", nargs="?", help="value of the _easyorder_session cookie (prompted if omitted)")
    sub.add_parser("logout", help="forget the saved session")
    sub.add_parser("whoami", help="account, locations and default location per weekday")

    s = sub.add_parser("week", help="my meals for a week (Mon-Fri)")
    s.add_argument("date", nargs="?", help="any date in the week (default: this week; next week on weekends)")

    s = sub.add_parser("day", help="my meal(s) for a day, plus every location's delivery")
    s.add_argument("date")

    s = sub.add_parser("menu", help="restaurants and items available for a day")
    s.add_argument("date")
    _add_where(s)
    s.add_argument("-v", "--verbose", action="store_true", help="show descriptions and all options")

    s = sub.add_parser("item", help="an item's details and customization options")
    s.add_argument("date")
    s.add_argument("menu_id", type=int)
    s.add_argument("item_id", type=int)
    _add_where(s)

    s = sub.add_parser("order", help="set my meal for a day (replaces my meal, or adds one)")
    s.add_argument("date")
    s.add_argument("menu_id", type=int)
    s.add_argument("item_id", type=int)
    s.add_argument("--opt", action="append", metavar="MOD=OPT",
                   help="customization, by id or name substring; repeatable. e.g. --opt bread=multigrain")
    s.add_argument("--note", default=None, help='special instructions (default: keep current note; "" clears)')
    _add_where(s)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--add", action="store_true", help="add an extra meal instead of replacing")
    g.add_argument("--piece", help="which of my meals to replace (id prefix shown by `day`)")
    s.add_argument("-n", "--dry-run", action="store_true", help="show what would happen, change nothing")

    for name, aliases, help_ in [("confirm", [], "set a day to Order (you'll receive the meal)"),
                                 ("unconfirm", ["dont-order"], "set a day to Don't order (keeps the selection)")]:
        s = sub.add_parser(name, aliases=aliases, help=help_)
        s.add_argument("date")
        s.add_argument("--piece")

    s = sub.add_parser("note", help="set special instructions on a day's meal")
    s.add_argument("date")
    s.add_argument("text", help='e.g. "No onions, please" ("" clears)')
    s.add_argument("--piece")

    s = sub.add_parser("skip", help="remove my meal(s) for a day")
    s.add_argument("date")
    s.add_argument("--piece")
    s.add_argument("-n", "--dry-run", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd in ("login", "set-session", "logout"):
            return _auth(args)
        return _run(Forkable(), args)
    except NotLoggedIn as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ForkableError as e:
        print(f"error: {e}", file=sys.stderr)
        if e.details and args.json:
            print(json.dumps(e.details, indent=2, default=str), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _auth(args) -> int:
    if args.cmd == "logout":
        C.clear_session()
        print(f"Removed {C.SESSION_FILE}")
        return 0
    if args.cmd == "set-session":
        cookie = args.cookie or getpass.getpass("Paste the _easyorder_session cookie value (input hidden): ")
        return _finish_login(GraphQLClient(cookie))
    if not (args.code or args.password):
        return _login_from_browser(args)

    email = args.email or input("Forkable email: ").strip()
    gql = GraphQLClient()
    if args.password:
        gql.login_with_password(email, getpass.getpass("Password: "), args.mfa)
        return _finish_login(gql)
    try:
        gql.request_login_code(email)
    except SsoRequired as e:
        print(f"{e}\n{SSO_FALLBACK}")
        return 1
    gql.login_with_code(email, input(f"Enter the 6-digit code Forkable emailed to {email}: "))
    return _finish_login(gql)


def _finish_login(gql: GraphQLClient, me: Optional[dict] = None, source: str = "") -> int:
    """Verify the session (unless `me` is already known), save it, and say who's logged in."""
    me = me or Forkable(gql).me
    path = C.save_session(gql.session_cookie, me["email"])
    print(f"Logged in as {me['fullName']} <{me['email']}>{source}; session saved to {path}")
    return 0


def _login_from_browser(args) -> int:
    """Find Forkable session cookies in every browser (or --browser), newest first, and use the first valid one."""
    from . import browser as B

    if sys.platform != "darwin":
        print(f"Browser import works on macOS only.\n{MANUAL_COOKIE}")
        return 1
    names = [args.browser] if args.browser else B.installed_browsers()
    cookies, safari_blocked = [], False
    for name in names:
        try:
            cookies += B.find_session_cookies(name, args.profile)
        except B.SafariNoAccess:
            safari_blocked = True
        except ForkableError:
            if args.browser:
                raise
    if not cookies:
        checked = [B.DISPLAY[n] for n in names if not (n == "safari" and safari_blocked)]
        if checked:
            print(f"No Forkable login found in {', '.join(checked)}.")
        if safari_blocked:
            print(SAFARI_BLOCKED)
        print(f"Log in at https://forkable.com/mc/ in your browser and retry, or:\n{MANUAL_COOKIE}")
        return 1

    cookies.sort(key=lambda c: c.last_access, reverse=True)
    allowed: Dict[str, bool] = {}  # Chromium browser -> user agreed to the Keychain prompt
    for c in cookies:
        where = B.DISPLAY[c.browser] + ("" if c.browser == "safari" else f" profile '{c.profile_name}'")
        if c.needs_keychain:
            if c.browser not in allowed:
                allowed[c.browser] = args.yes or _confirm_keychain(B.DISPLAY[c.browser], B.keychain_item(c.browser))
            if not allowed[c.browser]:
                continue
            try:
                B.decrypt(c)
            except ForkableError as e:
                print(f"  {where}: {e}")
                allowed[c.browser] = False
                continue
        gql = GraphQLClient(c.value)
        try:
            me = Forkable(gql).me
        except NotLoggedIn:
            print(f"  {where}: session expired, skipping")
            continue
        if args.email and me["email"].lower() != C.normalize_email(args.email):
            print(f"  {where}: logged in as {me['email']}, skipping")
            continue
        return _finish_login(gql, me, f" from {where}")
    print("No Forkable login imported. If your browser session expired, log in again there and retry, or:")
    print(MANUAL_COOKIE)
    return 1


def _confirm_keychain(app: str, item: str) -> bool:
    print(KEYCHAIN_NOTICE.format(app=app, item=item))
    if input("\nContinue? [Y/n] ").strip().lower() in ("n", "no"):
        print("OK, skipped.")
        return False
    return True


def _emit(obj) -> int:
    print(json.dumps(obj, indent=2, default=_json_default))
    return 0


def _show_result(fk: Forkable, args, result) -> int:
    """After a change: raw result with --json, else the day's updated meals."""
    if args.json:
        return _emit(result)
    _print_day(fk.day(args.date))
    return 0


def _run(fk: Forkable, args) -> int:
    if args.cmd == "whoami":
        me = fk.me
        if args.json:
            return _emit(me)
        names = {c["id"]: c["name"] for c in me["mealClubs"]}
        print(f"{me['fullName']} <{me['email']}> (user {me['id']})")
        print(f"Auto order: {'ON' if fk.auto_order else 'OFF'}"
              + ("" if fk.auto_order else " (meals Forkable suggests aren't ordered until you confirm them)"))
        print("Locations:")
        for cid, name in names.items():
            print(f"  {cid:>6}  {name}")
        if me.get("preferredLocations"):
            print("Default location:")
            for loc in sorted(me["preferredLocations"], key=lambda loc: loc["wday"]):
                print(f"  {DAYS[loc['wday']]}  {names.get(loc['clubId'], loc['clubId'])}")
        return 0

    if args.cmd == "week":
        days = fk.week(args.date)
        if args.json:
            return _emit([{"date": d.date, "selections": d.my_selections} for d in days])
        for d in days:
            _print_day(d)
        return 0

    if args.cmd == "day":
        day = fk.day(args.date)
        if args.json:
            return _emit({"date": day.date, "preferredClubId": day.preferred_club_id, "deliveries": day.deliveries})
        _print_day(day)
        for d in day.deliveries:
            tags = [t for t, on in [(d.service_window, bool(d.service_window)),
                                    ("default", d.club_id == day.preferred_club_id),
                                    ("locked", not d.editable)] if on]
            print(f"  club {d.club_id} {d.club_name} — delivery {d.id}, {len(d.menu_ids)} restaurants"
                  + (f" ({', '.join(tags)})" if tags else ""))
        return 0

    if args.cmd == "menu":
        delivery = fk.day(args.date).delivery_for(args.club, args.delivery, args.window)
        menus = fk.menus(delivery)
        if args.json:
            return _emit({"delivery": delivery, "menus": menus})
        print(f"{delivery.date:%a %b %d} at {delivery.club_name} (club {delivery.club_id}, delivery {delivery.id})"
              + ("" if delivery.editable else "  — LOCKED (past deadline)"))
        for s in delivery.my_selections:
            print(f"Current: {_sel_line(s)}")
        for m in menus:
            _print_menu(m, args.verbose)
        return 0

    if args.cmd == "item":
        delivery = fk.day(args.date).delivery_for(args.club, args.delivery, args.window)
        menu = fk.menu(delivery, args.menu_id)
        item = menu.item(args.item_id)
        if args.json:
            return _emit(item)
        print(f"{item.name} — {menu.name}  {_money(item.price)}{'' if item.available else '  [SOLD OUT]'}")
        if item.description:
            print(item.description)
        if item.ingredient_tags:
            print(f"tags: {', '.join(item.ingredient_tags)}")
        if not item.modifiers:
            print("(no customization options)")
        for m in item.modifiers:
            _print_modifier(m, indent=2)
        return 0

    if args.cmd == "order":
        plan = fk.plan_order(args.date, args.menu_id, args.item_id, _parse_opts(args.opt), args.club, args.note,
                             mode="add" if args.add else "auto", piece_id=args.piece,
                             delivery_id=args.delivery, window=args.window)
        d, old = plan.delivery, plan.old
        print(f"{plan.action.upper()}: {plan.item.name} from {plan.menu.name} at {d.club_name} on {d.date}")
        if plan.options:
            print("  " + "; ".join(f"{o['label']}: {o['value']}" for o in plan.options))
        if old:
            print(f"  replacing: {old.name} ({old.club_name})")
        if plan.instructions:
            print(f"  note: {plan.instructions}")
        conflicts = fk.restriction_conflicts(plan.item, plan.selections)
        if conflicts:
            print(f"  warning — dietary restriction conflicts: {conflicts}")
        if args.dry_run:
            print("  selectionsHash:", json.dumps(plan.selections))
            print("(dry run, nothing changed)")
            return 0
        return _show_result(fk, args, fk.execute_plan(plan))

    if args.cmd in ("confirm", "unconfirm", "dont-order"):
        return _show_result(fk, args, fk.set_confirmed(args.date, args.cmd == "confirm", args.piece))

    if args.cmd == "note":
        return _show_result(fk, args, fk.set_note(args.date, args.text, args.piece))

    if args.cmd == "skip":
        sels = fk.day(args.date).meals(args.piece)
        if not sels:
            print("No meal to remove.")
            return 0
        for s in sels:
            print(f"REMOVE: {_sel_line(s)}")
        if args.dry_run:
            print("(dry run, nothing changed)")
            return 0
        fk.remove_meals(sels)
        print("Done.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
