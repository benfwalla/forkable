"""High-level Forkable API: weekly selections, menus, item options, ordering.

Domain model (as Forkable uses it):
  * A *club* ("meal club") is a delivery location, e.g. "Acme HQ - 3rd Floor".
    You can belong to several; `preferredLocations` says which one is your default per weekday.
  * A *delivery* is one club + one date (+ service window). You get a delivery for every club
    you belong to, every day, whether or not you have a meal in it.
  * Each delivery has one *order* per restaurant (menu/venue) available that day.
  * A *piece* is one person's meal inside an order. "My selection" for a day = the piece
    with my userId across that day's deliveries.
  * A *selectionsHash* is the item customization: {modifierId: [optionId, ...]}.
    The web app sends [-1] for optional modifiers the user left empty.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union
from zoneinfo import ZoneInfo

from . import queries as Q
from .client import ForkableError, GraphQLClient

DateLike = Union[str, dt.date]


def _date(d: DateLike) -> dt.date:
    return d if isinstance(d, dt.date) else dt.date.fromisoformat(d)


def _squash(s: Optional[str]) -> str:
    """Collapse whitespace (menu names often contain stray newlines)."""
    return " ".join((s or "").split())


def _norm(s: str) -> str:
    return _squash(s).lower()


# -- result types -------------------------------------------------------------------


@dataclass
class Option:
    id: int
    name: str
    price: Optional[float]
    ingredient_tags: List[str]


def _option_list(options: List[Option]) -> str:
    return ", ".join(f"{o.id}={o.name}" for o in options)


@dataclass
class Modifier:
    id: int
    name: str
    min: int
    max: int
    required: bool
    hidden: bool
    options: List[Option]

    @classmethod
    def from_api(cls, m: dict) -> "Modifier":
        return cls(
            id=m["id"], name=_squash(m["name"]), min=m.get("min") or 0, max=m.get("max") or 0,
            required=bool(m.get("required")), hidden=bool(m.get("hidden")),
            options=[Option(o["id"], _squash(o["name"]), o.get("price"), o.get("ingredientTags") or [])
                     for o in m.get("options") or []],
        )


@dataclass
class MenuItem:
    id: int
    menu_id: int
    name: str
    description: str
    price: float
    section: str
    available: bool
    average_rating: Optional[float]
    ingredient_tags: List[str]
    image_url: Optional[str]
    modifiers: List[Modifier]

    def describe(self, selections: Dict[str, List[int]]) -> List[Dict[str, str]]:
        """selectionsHash -> [{label, value}], the same shape as Selection.options."""
        out = []
        for m in self.modifiers:
            ids = selections.get(str(m.id), [])
            names = [o.name for o in m.options if o.id in ids]
            if names:
                out.append({"label": m.name, "value": ", ".join(names)})
        return out


@dataclass
class Menu:
    id: int
    name: str
    venue_id: int
    venue_name: str
    capacity: Optional[int]
    usage: Optional[int]
    sold_out: bool
    tags: List[str]
    items: List[MenuItem]

    def item(self, item_id: int) -> MenuItem:
        for it in self.items:
            if it.id == item_id:
                return it
        raise ForkableError(f"Item {item_id} not found on menu {self.id} ({self.name})")


@dataclass
class Selection:
    """One of my meals (a piece)."""

    piece_id: str
    order_id: str
    delivery_id: int
    club_id: int
    club_name: str
    date: dt.date
    menu_id: int
    menu_name: str
    item_id: int
    name: str
    price: float
    state: str
    confirmed: bool
    auto_order: bool
    instructions: str
    selections: Dict[str, List[int]]
    options: List[Dict[str, str]]  # human readable [{label, value}]

    @property
    def short_id(self) -> str:
        return self.piece_id[:8]


@dataclass
class Delivery:
    id: int
    date: dt.date
    club_id: int
    club_name: str
    state: str
    read_only: bool
    past_deadline: bool
    afternoon: bool
    service_window: str  # lowercased, e.g. "lunch", "dinner"
    confirmed: bool  # the week view's Order / Don't order toggle
    menu_ids: List[int]
    my_selections: List[Selection]
    menu_venues: Dict[int, int]  # menu id -> venue id
    over_capacity_menu_ids: List[int]
    inactive_items: List[List[int]]  # [sectionId, itemId] pairs the restaurant switched off

    @property
    def editable(self) -> bool:
        return not self.read_only and not self.past_deadline

    def in_window(self, window: Optional[str]) -> bool:
        return not window or self.service_window == window.lower()


def _ambiguous(sels: List[Selection], date: dt.date) -> ForkableError:
    names = ", ".join(f"{s.short_id}={s.name}" for s in sels)
    return ForkableError(f"Several meals on {date}; say which one (piece id): {names}")


@dataclass
class Day:
    date: dt.date
    preferred_club_id: Optional[int]
    deliveries: List[Delivery]

    @property
    def my_selections(self) -> List[Selection]:
        return [s for d in self.deliveries for s in d.my_selections]

    def meals(self, piece_id: Optional[str] = None, required: bool = False) -> List[Selection]:
        """My meals, optionally narrowed to one piece id (a prefix is enough)."""
        sels = [s for s in self.my_selections if not piece_id or s.piece_id.startswith(piece_id)]
        if required and not sels:
            raise ForkableError(f"No meal on {self.date}" + (f" matching {piece_id}" if piece_id else ""))
        return sels

    def delivery_for(self, club_id: Optional[int] = None, delivery_id: Optional[int] = None,
                     window: Optional[str] = None) -> Delivery:
        """Pick a delivery: by id; else at `club_id`; else where I already have a meal; else my
        preferred club; else the only one. `window` ("lunch"/"dinner") narrows when a club has several.
        """
        if delivery_id is not None:
            for d in self.deliveries:
                if d.id == delivery_id:
                    return d
            raise ForkableError(f"No delivery {delivery_id} on {self.date}")
        pool = [d for d in self.deliveries if d.in_window(window)]
        if club_id is None:
            mine = [d for d in pool if d.my_selections]
            if mine:
                return mine[0]
            club_id = self.preferred_club_id
        for d in pool:
            if club_id is None or d.club_id == club_id:
                return d
        if len(pool) == 1:
            return pool[0]
        where = f"club {club_id}" if club_id else "any location"
        raise ForkableError(f"No delivery on {self.date} at {where}{f' for {window}' if window else ''}")


@dataclass
class OrderPlan:
    """What `order` will do. Build with plan_order, run with execute_plan."""

    action: str  # "add" | "replace" | "move"
    delivery: Delivery
    menu: Menu
    item: MenuItem
    selections: Dict[str, List[int]]
    instructions: str
    old: Optional[Selection]
    replace_delivery_ids: List[int]  # for "move": every location's delivery in that window

    @property
    def options(self) -> List[Dict[str, str]]:
        return self.item.describe(self.selections)


# -- client -------------------------------------------------------------------------


class Forkable:
    def __init__(self, gql: Optional[GraphQLClient] = None):
        self.gql = gql or GraphQLClient.from_env()
        self._me: Optional[dict] = None
        self._tzs: Dict[int, Optional[ZoneInfo]] = {}

    # --- identity ---

    @property
    def me(self) -> dict:
        if self._me is None:
            self._me = self.gql.execute(Q.ME)["me"]
        return self._me

    @property
    def user_id(self) -> int:
        return int(self.me["id"])

    def clubs(self) -> List[dict]:
        return self.me["mealClubs"]

    @property
    def auto_order(self) -> bool:
        """If False, meals Forkable pre-fills for you aren't ordered until you confirm them."""
        return bool(self.me.get("mealClubAutoOrder"))

    def preferred_club(self, date: DateLike) -> Optional[int]:
        wday = _date(date).isoweekday() % 7  # Forkable uses 0=Sun..6=Sat (Ruby wday)
        for loc in self.me.get("preferredLocations") or []:
            if loc["wday"] == wday:
                return loc["clubId"]
        return None

    def _tz(self, club_id: int) -> Optional[ZoneInfo]:
        if not self._tzs:
            for c in self.clubs():
                tz = (c.get("market") or {}).get("timezone")
                try:
                    self._tzs[c["id"]] = ZoneInfo(tz) if tz else None
                except Exception:
                    self._tzs[c["id"]] = None
        return self._tzs.get(club_id)

    def _local_date(self, iso: str, club_id: int) -> dt.date:
        tz = self._tz(club_id)
        if not tz:
            return dt.date.fromisoformat(iso[:10])
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz).date()

    # --- deliveries / selections ---

    def deliveries(self, start: DateLike, end: Optional[DateLike] = None) -> List[Delivery]:
        start = _date(start)
        end = _date(end) if end else start
        # Pad by a day: the server filters in UTC, we filter by the club's local date.
        pad = dt.timedelta(days=1)
        variables = {"from": (start - pad).isoformat(), "to": (end + pad).isoformat()}
        if self._me is None:
            data = self.gql.execute(Q.MY_DELIVERIES_AND_ME, variables)
            self._me = data["me"]
        else:
            data = self.gql.execute(Q.MY_DELIVERIES, variables)
        out = [self._delivery(d) for d in data["myDeliveries"]]
        return [d for d in out if start <= d.date <= end]

    def _delivery(self, d: dict) -> Delivery:
        date = self._local_date(d["forDeliveryAt"], d["mealClubId"])
        orders = d.get("orders") or []
        mine = [
            Selection(
                piece_id=p["id"], order_id=o["id"], delivery_id=d["id"], club_id=d["mealClubId"],
                club_name=d["club"]["name"], date=date, menu_id=p["menuId"], menu_name=_squash(o["menu"]["name"]),
                item_id=p["itemId"], name=p["name"].strip(), price=p.get("price") or 0.0,
                state=p.get("state") or "", confirmed=bool(p.get("isConfirmed")),
                auto_order=bool(p.get("autoOrder")), instructions=p.get("instructions") or "",
                selections=p.get("selections") or {}, options=p.get("attributes") or [],
            )
            for o in orders for p in o.get("pieces") or [] if p.get("userId") == self.user_id
        ]
        return Delivery(
            id=d["id"], date=date, club_id=d["mealClubId"], club_name=d["club"]["name"], state=d.get("state") or "",
            read_only=bool(d.get("isReadOnly")), past_deadline=bool(d.get("pastLateOrderDeadline")),
            afternoon=bool(d.get("afternoon")),
            service_window=((d.get("serviceWindow") or {}).get("name") or "").lower(),
            confirmed=bool(d.get("userConfirmed")), menu_ids=d.get("availableMenuIds") or [], my_selections=mine,
            menu_venues={o["menu"]["id"]: o["venue"]["id"] for o in orders if o.get("venue")},
            over_capacity_menu_ids=[o["menu"]["id"] for o in orders if o.get("isOverVenueCapacity")],
            inactive_items=[[s["id"], it["id"]] for o in orders for s in o["menu"].get("sections") or []
                            for it in s.get("items") or [] if not (s.get("active") and it.get("active"))],
        )

    def day(self, date: DateLike) -> Day:
        date = _date(date)
        deliveries = self.deliveries(date)
        return Day(date, self.preferred_club(date), deliveries)

    def week(self, date: Optional[DateLike] = None) -> List[Day]:
        """Mon-Fri of the week containing `date` (default: this week, or next week on weekends)."""
        if date:
            date = _date(date)
        else:
            date = dt.date.today()
            if date.weekday() >= 5:
                date += dt.timedelta(days=7 - date.weekday())
        monday = date - dt.timedelta(days=date.weekday())
        by_date: Dict[dt.date, List[Delivery]] = {}
        for d in self.deliveries(monday, monday + dt.timedelta(days=4)):
            by_date.setdefault(d.date, []).append(d)
        return [Day(day, self.preferred_club(day), by_date.get(day, []))
                for day in (monday + dt.timedelta(days=i) for i in range(5))]

    # --- menus ---

    def menus(self, delivery: Delivery, menu_ids: Optional[List[int]] = None) -> List[Menu]:
        """Menus for a delivery (all of them, or just `menu_ids`), with sold-out status."""
        ids = menu_ids or delivery.menu_ids
        if not ids:
            return []
        day = delivery.date.isoformat()
        data = self.gql.execute(Q.MENUS_WITH_USAGE, {
            "ids": ids, "clubId": delivery.club_id, "date": day,
            "venueIds": [delivery.menu_venues[m] for m in ids if m in delivery.menu_venues],
        })
        usage = data.get("venueUsage") or {}
        shift = "pm" if delivery.afternoon else "am"
        inactive = {tuple(x) for x in delivery.inactive_items}

        menus = []
        for m in data["menus"]:
            venue = m.get("venue") or {}
            used = (usage.get(str(venue.get("id"))) or {}).get(day, {}).get(shift)
            cap = venue.get("capacity")
            # Same rule as the web app's soldOutMenuIds getter.
            sold_out = m["id"] in delivery.over_capacity_menu_ids or bool(cap and used is not None and cap <= used)
            items = [
                MenuItem(
                    id=it["id"], menu_id=m["id"], name=it["name"].strip(),
                    description=(it.get("description") or "").strip(), price=it.get("price") or 0.0,
                    section=_squash(s.get("name")), available=not sold_out and (s["id"], it["id"]) not in inactive,
                    average_rating=it.get("averageRating"), ingredient_tags=it.get("ingredientTags") or [],
                    image_url=it.get("imageUrl"), modifiers=[Modifier.from_api(x) for x in it.get("modifiers") or []],
                )
                for s in m.get("sections") or [] for it in s.get("items") or []
            ]
            menus.append(Menu(
                id=m["id"], name=_squash(m.get("displayName") or m["name"]), venue_id=venue.get("id"),
                venue_name=_squash(venue.get("displayName") or venue.get("name")), capacity=cap, usage=used,
                sold_out=sold_out, tags=m.get("tags") or [], items=items,
            ))
        return menus

    def menu(self, delivery: Delivery, menu_id: int) -> Menu:
        if menu_id in delivery.menu_ids:
            for m in self.menus(delivery, [menu_id]):
                return m
        raise ForkableError(f"Menu {menu_id} is not available for delivery {delivery.id} ({delivery.date})")

    # --- customization ---

    @staticmethod
    def build_selections(item: MenuItem, choices: Optional[Dict[Union[int, str], Any]] = None) -> Dict[str, List[int]]:
        """Turn user choices into a validated selectionsHash.

        `choices` maps a modifier (id, or case-insensitive substring of its name) to one or
        more options (ids, or name substrings). e.g. {"bagel": "multigrain", 26: 22}.
        Optional modifiers that aren't chosen get [-1], matching the web app.
        """
        choices = dict(choices or {})
        result: Dict[str, List[int]] = {}
        for mod in item.modifiers:
            key = _match_key(mod, choices)
            if key is None:
                if mod.min > 0 and not mod.hidden:
                    raise ForkableError(f"'{mod.name}' (modifier {mod.id}) is required. "
                                        f"Options: {_option_list(mod.options)}")
                result[str(mod.id)] = [-1]
                continue
            wanted = choices.pop(key)
            wanted = wanted if isinstance(wanted, (list, tuple)) else [wanted]
            picked = [_match_option(mod, w) for w in wanted]
            if mod.max and len(picked) > mod.max:
                raise ForkableError(f"'{mod.name}' allows at most {mod.max} option(s)")
            if len(picked) < mod.min:
                raise ForkableError(f"'{mod.name}' needs at least {mod.min} option(s)")
            result[str(mod.id)] = picked
        if choices:
            raise ForkableError(f"Unknown modifier(s) for '{item.name}': {list(choices)}")
        return result

    def restriction_conflicts(self, item: MenuItem, selections: Dict[str, List[int]]) -> Any:
        """Server-side check against your dietary restrictions (what the UI warns about)."""
        r = self.gql.execute(Q.MEAL_RESTRICTIONS, {
            "userId": self.user_id, "menuId": item.menu_id, "itemId": item.id,
            "customization": json.dumps(selections, separators=(",", ":")),
        })
        return r["mealRestrictions"]["conflicts"]

    # --- mutations ---

    def add_piece(self, delivery_id: int, menu_id: int, item_id: int, selections: Dict[str, List[int]],
                  instructions: str = "") -> dict:
        """Add a NEW meal to a delivery (`addPiece`). Use when you have no meal that day."""
        return self.gql.mutate("addPiece", {
            **_piece_input(menu_id, item_id, selections, instructions),
            "deliveryId": delivery_id, "userId": self.user_id, "myMeals": True,
        }, Q.PIECE_MUTATION_SELECTION)

    def replace_piece(self, delivery_id: int, old_piece_id: str, menu_id: int, item_id: int,
                      selections: Dict[str, List[int]], instructions: str = "") -> dict:
        """Swap an existing meal for another item/customization (`replacePiece`)."""
        return self.gql.mutate("replacePiece", {
            **_piece_input(menu_id, item_id, selections, instructions),
            "deliveryId": delivery_id, "oldPieceId": old_piece_id, "myMeals": True,
        }, Q.PIECE_MUTATION_SELECTION)

    def replace_all_pieces(self, delivery_ids: List[int], target_delivery_id: int, menu_id: int, item_id: int,
                           selections: Dict[str, List[int]], instructions: str = "") -> dict:
        """Replace my meals in `delivery_ids` with one new meal in `target_delivery_id` (`replaceAllPieces`).

        The web app uses this to switch location for a day: deliveryIds = every location's
        delivery that day, newPiece.deliveryId = the location you picked.
        """
        return self.gql.mutate("replaceAllPieces", {
            "deliveryIds": delivery_ids,
            "newPiece": {**_piece_input(menu_id, item_id, selections, instructions), "deliveryId": target_delivery_id},
            "myMeals": True,
        }, f"errors deliveries {{ {Q.DELIVERY_FIELDS} }}")

    def remove_piece(self, order_id: str, piece_id: str) -> dict:
        """Remove (skip) a meal (`removePiece`)."""
        return self.gql.mutate("removePiece", {"orderId": order_id, "pieceId": piece_id, "myMeals": True},
                               Q.PIECE_MUTATION_SELECTION)

    def confirm_delivery(self, delivery_id: int, confirm: bool = True) -> dict:
        """The week view's Order (True) / Don't order (False) toggle (`confirmDelivery`).

        "Don't order" keeps the selected meal but you won't receive it. addPiece auto-confirms.
        """
        return self.gql.mutate("confirmDelivery", {"deliveryId": delivery_id, "confirm": confirm,
                                                   "changeFrom": "dashboard"}, Q.PIECE_MUTATION_SELECTION)

    # --- day-level operations ---

    def set_confirmed(self, date: DateLike, confirm: bool = True, piece_id: Optional[str] = None) -> List[dict]:
        """Order / Don't order for the day's meal(s). Confirmation is per delivery."""
        sels = self.day(date).meals(piece_id, required=True)
        return [self.confirm_delivery(did, confirm) for did in dict.fromkeys(s.delivery_id for s in sels)]

    def set_note(self, date: DateLike, instructions: str, piece_id: Optional[str] = None) -> dict:
        """Change special instructions on the day's meal, keeping item and options.

        The web app does this with replacePiece on the same item.
        """
        day = self.day(date)
        sels = day.meals(piece_id, required=True)
        if len(sels) > 1:
            raise _ambiguous(sels, day.date)
        s = sels[0]
        return self.replace_piece(s.delivery_id, s.piece_id, s.menu_id, s.item_id, s.selections, instructions)

    def remove_meals(self, selections: List[Selection]) -> List[dict]:
        return [self.remove_piece(s.order_id, s.piece_id) for s in selections]

    def skip(self, date: DateLike, piece_id: Optional[str] = None) -> List[dict]:
        """Remove my meal(s) on `date` (all of them, or just `piece_id`)."""
        return self.remove_meals(self.day(date).meals(piece_id))

    def plan_order(self, date: DateLike, menu_id: int, item_id: int,
                   choices: Optional[Dict[Union[int, str], Any]] = None, club_id: Optional[int] = None,
                   instructions: Optional[str] = None, mode: str = "auto", piece_id: Optional[str] = None,
                   delivery_id: Optional[int] = None, window: Optional[str] = None) -> OrderPlan:
        """Work out what `order` would do, without changing anything.

        mode: "auto" (replace my meal if I have one, else add), "add" (extra meal), "replace".
        By default it replaces my meal that day even at another location (the web app's
        "Replace meal above"). instructions=None keeps the replaced meal's note; "" clears it.
        """
        if mode not in ("auto", "add", "replace"):
            raise ForkableError(f"Unknown mode {mode!r}")
        day = self.day(date)
        if delivery_id is not None or club_id is not None:
            target = day.delivery_for(club_id, delivery_id, window)
        else:
            # Prefer the delivery where my meal is (or my default location) if it offers this menu,
            # else whichever location that day does.
            current = day.delivery_for(window=window)
            candidates = [d for d in day.deliveries if menu_id in d.menu_ids and d.in_window(window)]
            target = current if current in candidates else (candidates[0] if candidates else current)
        if menu_id not in target.menu_ids:
            raise ForkableError(f"Menu {menu_id} isn't offered at {target.club_name} on {day.date}")
        if not target.editable:
            raise ForkableError(f"{target.club_name} on {day.date} is past its ordering deadline")

        menu = self.menu(target, menu_id)
        item = menu.item(item_id)
        if not item.available:
            raise ForkableError(f"'{item.name}' from {menu.name} is sold out / unavailable")
        selections = self.build_selections(item, choices)

        # A new meal replaces my meal in the same service window (lunch vs dinner).
        window_deliveries = [d for d in day.deliveries if d.service_window == target.service_window]
        old = None
        if piece_id:
            old = day.meals(piece_id, required=True)[0]
        elif mode != "add":
            existing = [s for d in window_deliveries for s in d.my_selections]
            same = [s for s in existing if s.delivery_id == target.id]
            if len(same) > 1 or (not same and len(existing) > 1):
                raise _ambiguous(existing, day.date)
            old = (same or existing or [None])[0]
        if mode == "replace" and not old:
            raise ForkableError(f"No meal on {day.date} to replace")

        action = "add" if old is None else "replace" if old.delivery_id == target.id else "move"
        if instructions is None:
            instructions = old.instructions if old else ""
        return OrderPlan(action=action, delivery=target, menu=menu, item=item, selections=selections,
                         instructions=instructions, old=old,
                         replace_delivery_ids=[d.id for d in window_deliveries])

    def execute_plan(self, plan: OrderPlan) -> dict:
        args = (plan.item.menu_id, plan.item.id, plan.selections, plan.instructions)
        if plan.action == "add":
            return self.add_piece(plan.delivery.id, *args)
        if plan.action == "replace":
            return self.replace_piece(plan.delivery.id, plan.old.piece_id, *args)
        # move: same call the web app makes when you pick a restaurant at another location
        return self.replace_all_pieces(plan.replace_delivery_ids, plan.delivery.id, *args)

    def order(self, *args, **kwargs) -> dict:
        """Set my meal for a day: plan_order(...) then execute_plan(...). Same arguments as plan_order."""
        return self.execute_plan(self.plan_order(*args, **kwargs))


# -- helpers ------------------------------------------------------------------------


def _piece_input(menu_id: int, item_id: int, selections: Dict[str, List[int]], instructions: str) -> dict:
    """Fields every piece mutation sends, as the web app does."""
    return {"menuId": menu_id, "itemId": item_id, "instructions": instructions, "selectionsHash": selections,
            "fromTopRated": False, "topRatedType": None}


def _match_key(mod: Modifier, choices: dict):
    for k in choices:
        if (isinstance(k, int) or str(k).isdigit()) and int(k) == mod.id:
            return k
    for k in choices:
        if not str(k).isdigit() and _norm(str(k)) in _norm(mod.name):
            return k
    return None


def _match_option(mod: Modifier, want: Union[int, str]) -> int:
    if isinstance(want, int) or str(want).lstrip("-").isdigit():
        oid = int(want)
        if oid == -1 or any(o.id == oid for o in mod.options):
            return oid
        raise ForkableError(f"Option {oid} isn't valid for '{mod.name}'")
    exact = [o for o in mod.options if _norm(o.name) == _norm(want)]
    partial = exact or [o for o in mod.options if _norm(want) in _norm(o.name)]
    if len(partial) == 1:
        return partial[0].id
    raise ForkableError(f"'{want}' is {'ambiguous' if partial else 'not an option'} for '{mod.name}': "
                        f"{_option_list(partial or mod.options)}")
