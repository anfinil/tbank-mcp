"""The travel booking bodies must match what the real app sends.

None of these three calls can ever be exercised live: `orders/create` reserves a
real seat, `orders/pay` and `travel_pay` move real money. So the only way they are
verified at all is here — by building the body the code WOULD send from recorded
inputs and comparing it, field by field, against the body the app actually sent.

Every bug in this repo's booking history came from a body that looked right:
`isSuspicious` baked into a template, JSON posted to a form endpoint, a dropped
`X-App-*` header answering 406. These are the same class, and they are silent —
the bank answers with a code, not with «you forgot carSearchId».

Three things here are counter-intuitive enough that they are asserted by name,
because a future tidy-up would «fix» each of them:

  * refund/calculate takes `TicketIds` and refund takes `ticketIds`. Same ids,
    different capitalisation, one API.
  * `segmentId` is invented by the CLIENT — it appears in no response anywhere in
    the capture — so it must be a fresh uuid, not looked up.
  * a flight's charge is fare + seats + check-in, three numbers, and the seat
    price keeps the JSON type the seat map gave it.

The contract lives in tests/fixtures/travel.json (real structure and protocol
values, synthetic personal ones), so this runs on any machine. When the gitignored
capture IS present the fixture is additionally checked against it, so it cannot
drift away from what the app really sends.

    python3 tests/test_travel_booking_bodies.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TBANK_EVENTS", os.path.join(tempfile.mkdtemp(), "events.jsonl"))
os.environ.setdefault("TBANK_ATTEMPTS",
                      os.path.join(tempfile.gettempdir(), "tbank-test-attempts.jsonl"))
os.environ.setdefault("TBANK_TRACE_FILE",
                      os.path.join(tempfile.gettempdir(), "tbank-test-calls.jsonl"))

from tbank_mcp import server  # noqa: E402
from tbank_mcp.client import MobileSession, TbankApiError  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "travel.json")

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


FX = fixture()


def payload(name):
    """What a client method receives. The fixture keeps whole responses as the app
    saw them, and the travel hosts wrap theirs in an envelope that _unwrap strips —
    so a test that fed the raw record to the code under test would be testing a
    shape the code never sees."""
    record = FX[name]
    return record["payload"] if isinstance(record, dict) and "payload" in record else record

PEOPLE = [
    {"first": "Иван", "last": "Петров", "middle": "Сергеевич",
     "firstEn": "IVAN", "lastEn": "PETROV", "middleEn": "SERGEEVICH",
     "birthDate": "1990-01-31", "number": "1234567890", "sex": "male"},
    {"first": "Мария", "last": "Петрова", "middle": "Ивановна",
     "firstEn": "MARIA", "lastEn": "PETROVA", "middleEn": "IVANOVNA",
     "birthDate": "1992-02-02", "number": "1234567891", "sex": "female"},
]


def shape(node, path="", out=None):
    """Every leaf path with its JSON type — the thing that must not drift."""
    out = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            shape(v, f"{path}.{k}", out)
    elif isinstance(node, list):
        if node:
            shape(node[0], f"{path}[]", out)
        else:
            out[path + "[]"] = "empty"
    else:
        out[path] = "null" if node is None else type(node).__name__
    return out


def compare_shape(name, ours, theirs, ignore=()):
    a, b = shape(ours), shape(theirs)
    for key in sorted(set(a) | set(b)):
        if any(key.startswith(i) for i in ignore):
            continue
        if key not in a:
            failures.append(f"{name}: MISSING field {key} (app sends it)")
        elif key not in b:
            failures.append(f"{name}: EXTRA field {key} (app does not send it)")
        elif a[key] != b[key] and "null" not in (a[key], b[key]):
            failures.append(f"{name}: {key} type {a[key]}, app sends {b[key]}")


# ---- rail: orders/create ---------------------------------------------------

def test_rail_order_body():
    """The order body we build == the one the app posted, field for field."""
    real = FX["train_order_create_request"]
    search_seg = FX["train_search_response"]["directions"][0]["ways"][0]["segments"][0]
    cars = FX["train_cars_response"]
    real_seg = real["ways"][0][0]
    real_group = real_seg["placeGroups"][0]
    wanted = [(real_group["carNumber"],
               p["places"][0]["placeNumber"]) for p in real_group["passengers"]]

    hits = server._find_places(cars["cars"], wanted)
    ways = server._rail_ways(search_seg, cars["carSearchId"], hits, PEOPLE)
    ours = {"ways": ways, "customer": {"phone": "+79991234567",
                                       "email": "user@example.com"}}
    compare_shape("orders/create", ours, real)

    seg = ours["ways"][0][0]
    check(seg["carSearchId"] == cars["carSearchId"],
          "orders/create: carSearchId must come from the CARS response, not the search")
    check(seg["origin"] == real_seg["origin"],
          f"orders/create: origin {seg['origin']!r} != app's {real_seg['origin']!r} "
          "— it is the SEGMENT's station, not the searched city")
    group = seg["placeGroups"][0]
    for field in ("type", "carType", "gender", "serviceClass",
                  "bookFullCompartmentType"):
        check(group[field] == real_group[field],
              f"orders/create: placeGroups[0].{field} = {group[field]!r}, "
              f"app sends {real_group[field]!r}")
    check(len(seg["segmentId"]) == 36 and seg["segmentId"].count("-") == 4,
          "orders/create: segmentId must be a fresh uuid — it exists in no response")
    other = server._rail_ways(search_seg, cars["carSearchId"], hits, PEOPLE)
    check(other[0][0]["segmentId"] != seg["segmentId"],
          "orders/create: segmentId must be NEW per order, not a constant")
    places = [p["places"][0]["placeNumber"] for p in group["passengers"]]
    check(places == [n for _, n in wanted],
          f"orders/create: places {places} lost the requested order {wanted}")


def test_rail_seat_order_is_passenger_order():
    """Passenger i must get seat i — a reshuffle seats the wrong person."""
    cars = FX["train_cars_response"]
    car = cars["cars"][0]
    numbers = [p["number"] for g in car["places"] for p in g["places"]][:2]
    wanted = [(car["number"], numbers[1]), (car["number"], numbers[0])]
    hits = server._find_places(cars["cars"], wanted)
    seg = FX["train_search_response"]["directions"][0]["ways"][0]["segments"][0]
    ways = server._rail_ways(seg, cars["carSearchId"], hits, PEOPLE)
    passengers = ways[0][0]["placeGroups"][0]["passengers"]
    got = [(p["documentWithCustomerIndex"]["document"]["name"]["first"],
            p["places"][0]["placeNumber"]) for p in passengers]
    check(got == [(PEOPLE[0]["first"], numbers[1]), (PEOPLE[1]["first"], numbers[0])],
          f"seat order: {got} — passenger i must keep seat i")


def test_rail_taken_seat_is_named():
    """A seat that is gone must be reported by name, not as a bank error code."""
    cars = FX["train_cars_response"]
    try:
        server._find_places(cars["cars"], [("99", "999")])
    except Exception as e:  # noqa: BLE001
        check("99/999" in str(e),
              f"taken seat: error must name the seat, got {e}")
    else:
        failures.append("taken seat: a nonexistent place was accepted")


# ---- rail: the refund key-case trap ----------------------------------------

def test_refund_key_case_differs():
    """calculate wants TicketIds, refund wants ticketIds. Both spellings pinned."""
    sent = {}

    class S(MobileSession):
        def __init__(self):
            pass

        def _call_read(self, key, *, overrides=None, body=None, path_override=None,
                       return_response=False):
            sent[key] = body
            return {}

    s = S()
    ids = ["t1", "t2"]
    s.train_refund_calc("order-1", ids)
    s.train_refund("order-1", ids)
    calc, refund = sent["train_refund_calc"], sent["train_refund"]
    check("TicketIds" in calc,
          f"refund/calculate must send TicketIds (capital T), sent {sorted(calc)}")
    check("ticketIds" in refund,
          f"refund must send ticketIds (lowercase t), sent {sorted(refund)}")
    check("TicketIds" in FX["train_refund_calc_request"],
          "fixture drift: the captured calculate no longer uses TicketIds")
    check("ticketIds" in FX["train_refund_request"],
          "fixture drift: the captured refund no longer uses ticketIds")
    compare_shape("refund/calculate", calc, FX["train_refund_calc_request"])
    compare_shape("refund", refund, FX["train_refund_request"])


# ---- flights: travel_pay ---------------------------------------------------

def test_flight_pay_body():
    real = FX["flight_pay_request"]
    prelim = payload("flight_preliminary_response")
    seatmaps = payload("flight_seatmaps_response")
    # NOT coerced: the API sends whole roubles as an int and the body has to carry
    # the same JSON type, so the test holds the raw value the way the code does.
    checkin = payload("flight_checkin_response")["price"]["amount"]

    real_seats = [f"{s['flights'][0]['row']}{s['flights'][0]['letter']}"
                  for s in real["seats"]]
    blocks, seat_sum = server._seat_blocks(real_seats, PEOPLE, seatmaps, prelim)
    fare = float(prelim["offers"][0]["price"]["amount"])
    total = round(fare + seat_sum + float(checkin), 2)

    ours = {
        "pay_request": {"moneyAmount": total, "currency": "RUB",
                        "attachCard": False, "account": "0000000000"},
        "timezone": "+180", "screen_resolution": "420x912",
        "device_platform": "iOS",
        "contact_info": {"email": "user@example.com", "phone": "+79991234567"},
        "booking": {"persons": [server._person_block(p) for p in PEOPLE],
                    "offer_uuid": prelim["offers"][0]["uuid"]},
        "payAdditionalInfo": dict(server._FLIGHT_PAY_INFO),
        "seats": blocks,
        "checkin": {"price": {"amount": checkin, "currency": "RUB"},
                    "seatStrategies": ["skip"], "placements": [],
                    "summaryText": "Регистрация на рейс",
                    "summaryTextWithoutSeats": "Регистрация на рейс"},
    }
    compare_shape("travel_pay", ours, real,
                  ignore=(".booking.persons[].bonus_card",))

    check(total == real["pay_request"]["moneyAmount"],
          f"travel_pay: charge {total} != the app's {real['pay_request']['moneyAmount']} "
          "— it is fare + seats + check-in, three numbers")
    leg = ours["seats"][0]["flights"][0]
    for field in ("operatingCarrier", "marketingCarrier", "number", "date"):
        check(field in leg,
              f"travel_pay: a seat must name its flight — {field} missing")
    real_leg = real["seats"][0]["flights"][0]
    check(type(leg["price"]["amount"]) is type(real_leg["price"]["amount"]),
          "travel_pay: seat price must keep the seat map's JSON type")
    person = ours["booking"]["persons"][0]
    check(person["name"].isupper() and person["surname"].isupper(),
          "travel_pay: names go on a ticket in capitals")
    check(person["name"] == PEOPLE[0]["firstEn"],
          "travel_pay: the Latin spelling must come from the bank, not be guessed")


def test_flight_bonus_card_both_ways():
    """The same booking carries a passenger WITH an airline card and one without.

    The app sends an object for the first and null for the second; always sending
    null would silently stop the miles from being credited, and always sending an
    object would be a field the app never sends."""
    real_people = payload("flight_preliminary_response") and FX["flight_pay_request"]["booking"]["persons"]
    with_card = next((p for p in real_people if p.get("bonus_card")), None)
    without = next((p for p in real_people if not p.get("bonus_card")), None)
    check(with_card is not None and without is not None,
          "fixture drift: the captured booking no longer has both bonus_card variants")
    if not (with_card and without):
        return
    carded = dict(PEOPLE[0], bonus_card={"carrier_code": "su", "number": "1000000001"})
    block = server._person_block(carded)
    check(block["bonus_card"] == {"carrier_code": "SU", "number": "1000000001"},
          f"bonus_card: built {block['bonus_card']!r}, app sends "
          f"{ {'carrier_code': 'SU', 'number': '1000000001'} !r}")
    check(server._person_block(PEOPLE[1])["bonus_card"] is None,
          "bonus_card: a passenger without a card must send null, not an empty object")
    check(shape(block)[".bonus_card.number"] == shape(with_card)[".bonus_card.number"],
          "bonus_card: number must be a string, as the app sends it")


def test_flight_seats_refuse_connections():
    """One seat per passenger cannot describe two legs — say so, do not guess."""
    prelim = json.loads(json.dumps(payload("flight_preliminary_response")))
    seg = prelim["flights"][0]["flightSegments"][0]
    prelim["flights"][0]["flightSegments"] = [seg, dict(seg)]
    try:
        server._seat_blocks(["13A"], PEOPLE[:1], payload("flight_seatmaps_response"), prelim)
    except Exception as e:  # noqa: BLE001
        check("seats" in str(e).lower() or "перелёт" in str(e),
              f"connection: unhelpful error {e}")
    else:
        failures.append("connection: seats were silently applied to one leg only")


# ---- passengers on the money path -----------------------------------------

class DocSession:
    """A session whose document store holds the owner's passport AND a relative's."""

    def __init__(self, owner_bd="1990-01-31", entries=None, brief_bd="1990-01-31"):
        self._entries = entries if entries is not None else [
            {"value": {"serial": {"value": "1234"}, "number": {"value": "567890"},
                       "person": {"firstName": {"value": "Иван"},
                                  "lastName": {"value": "Петров"},
                                  "birthDate": {"value": owner_bd}}}},
            # A relative — different birthDate, LONGER number (so «longest wins»
            # would pick THIS one without the owner filter).
            {"value": {"serial": {"value": "9999"}, "number": {"value": "88888888"},
                       "person": {"firstName": {"value": "Тёща"},
                                  "lastName": {"value": "Петрова"},
                                  "birthDate": {"value": "1955-05-05"}}}},
        ]
        self._brief_bd = brief_bd

    def identity_documents(self):
        return {"RusNationalID": self._entries}

    def identity_brief(self):
        return {"birthDate": {"value": self._brief_bd}} if self._brief_bd else {}


def test_own_passenger_is_the_owner_not_a_relative():
    """documents() filters relatives by birthDate; the money path must too.

    The store holds the owner (1990) and the mother-in-law (1955, longer number).
    `max(len(number))` alone would pick the relative — putting a stranger's
    passport on the ticket. With the owner filter it must pick the owner's.
    """
    who = server._own_passenger(DocSession())
    check(who["number"] == "1234567890",
          f"must pick the OWNER's passport (serial+number), got {who['number']}")
    check(who["first"] == "Иван",
          f"must be the owner, not the relative: {who['first']}")

    # If nothing matches the holder, refuse rather than guess.
    orphan = DocSession(owner_bd="2000-01-01", brief_bd="1990-01-31")
    try:
        server._own_passenger(orphan)
        check(False, "a store with no owner-matching passport must refuse")
    except TbankApiError as e:
        check(e.result_code == "PASSENGER_AMBIGUOUS",
              f"the refusal must name itself: {e.result_code}")
    print("  passengers: «me» is the owner's passport, ambiguity refused")


def test_a_minor_is_refused_on_the_booking_path():
    """The booking hardcodes adult fare + RussianPassport; a child booked there is
    charged an adult fare with the wrong document, and no capture verifies a child
    booking. So a minor must be refused, on both «me» and explicit passengers.

    Ages are computed against today's clock (not a dated literal), so the birth
    dates here are derived from date.today() and stay correct every year."""
    from datetime import date
    today = date.today()
    child_bd = today.replace(year=today.year - 10).isoformat()
    adult_bd = today.replace(year=today.year - 40).isoformat()

    # «me» that is a minor → refuse.
    minor_me = DocSession(owner_bd=child_bd, brief_bd=child_bd)
    try:
        server._passengers(minor_me, "me")
        check(False, "a minor account holder must be refused on the booking path")
    except TbankApiError as e:
        check(e.result_code == "PASSENGER_MINOR", f"wrong refusal: {e.result_code}")

    # Explicit child passenger → refuse, naming which one.
    spec = json.dumps([{"first": "А", "last": "Б", "birthDate": adult_bd, "number": "1"},
                       {"first": "Д", "last": "Е", "birthDate": child_bd, "number": "2"}])
    try:
        server._passengers(DocSession(), spec)
        check(False, "an explicit child passenger must be refused")
    except TbankApiError as e:
        check(e.result_code == "PASSENGER_MINOR", f"wrong refusal: {e.result_code}")

    # All-adult explicit list passes.
    ok = server._passengers(DocSession(), json.dumps(
        [{"first": "А", "last": "Б", "birthDate": adult_bd, "number": "1"}]))
    check(len(ok) == 1 and ok[0]["birthDate"] == adult_bd,
          f"an adult passenger must pass: {ok}")
    print("  passengers: a minor is refused (me and explicit), adults pass")


# ---- tpay ------------------------------------------------------------------

def test_tpay_init_shape():
    real = FX["tpay_init_request"]
    for field in ("type", "productRequestId", "productId", "fingerprint",
                  "scenario", "theme"):
        check(field in real, f"fixture drift: INIT_TPW lost {field}")
    check(real["type"] == "INIT_TPW", "fixture drift: INIT_TPW type changed")
    check(MobileSession.TPAY_THEME == real["theme"],
          f"tpay: theme {MobileSession.TPAY_THEME} != captured {real['theme']}")
    check(MobileSession.TPAY_VIEWPORT ==
          (real["fingerprint"]["screen_width"], real["fingerprint"]["screen_height"]),
          "tpay: viewport differs from the captured fingerprint")
    check(MobileSession.TPAY_SSO_CLIENT == "tinkoff-pay-web",
          "tpay: the SSO client id is what the gateway keys the code on")


def test_tpay_flow_sends_account_step_and_correct_headers():
    """Drive the real tpay_pay(dry_run) through a recording jar.

    Two capture-verified facts the old code got wrong:
      * ACCOUNT_TPW must be sent after TOKEN_TPW, else /account never leaves NEW
        and the poll times out — after the order already holds a payment timer.
      * /status keys off the PRODUCT request id with `T-Request-Id`, while /account
        keys off the SESSION id with `T-Session-Id`; the old code sent
        `T-Session-Id=sessionId` on both.
    Also: every tpay request carries the WEBVIEW UA, not the native one.
    """
    from tbank_mcp.client import MobileSession

    class Resp:
        def __init__(self, body=b"", js=None):
            self.content = body
            self.status_code = 200
            self._js = js
            self.text = ""

        def json(self):
            if self._js is None:
                raise ValueError("no json")
            return self._js

        def raise_for_status(self):
            pass

    class RecJar:
        def __init__(self):
            self.reqs = []

        def _answer(self, method, url, headers, body):
            leaf = url.split("/api/v2/tpayid/")[-1].split("?")[0]
            self.reqs.append({"method": method, "leaf": leaf,
                              "headers": dict(headers), "body": body})
            if leaf == "session":
                t = (body or {}).get("type")
                if t == "INIT_TPW":
                    return Resp(b"{}", {"sessionId": "SID-1", "state": "STATE-1"})
                return Resp(b"{}", {})           # TOKEN/ACCOUNT/PAY
            if leaf == "token":
                return Resp(b"")                 # zero-length body
            if leaf == "account":
                return Resp(b"{}", {"status": "ACCOUNT",
                                    "accounts": [{"id": "a"}], "cards": [{"cardId": "c"}],
                                    "brandInfo": {"brandName": "Поезда"}})
            if leaf == "status":
                return Resp(b"{}", {"amount": 1000})
            return Resp(b"{}", {})

        def post(self, url, json=None, headers=None, timeout=None):
            return self._answer("POST", url, headers or {}, json)

        def get(self, url, headers=None, timeout=None):
            return self._answer("GET", url, headers or {}, None)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    s = MobileSession.__new__(MobileSession)
    s.sso_login_cookie = "SSO_SESSION=x"
    s.platform = "ios"
    jar = RecJar()
    s._fresh_jar = lambda: jar
    s._tpay_sso_code = lambda j, state, url: "CODE-1"

    out = s.tpay_pay("https://tpay.tbank.ru/PRID-1/17/tpay", dry_run=True)
    check(out.get("dry_run") is True and out["accounts"] == [{"id": "a"}],
          f"dry_run must return the payment methods: {out}")

    session_types = [r["body"]["type"] for r in jar.reqs if r["leaf"] == "session"]
    check(session_types == ["INIT_TPW", "TOKEN_TPW", "ACCOUNT_TPW"],
          f"the session sequence must include ACCOUNT_TPW after TOKEN_TPW: {session_types}")

    status_req = next((r for r in jar.reqs if r["leaf"] == "status"), None)
    check(status_req is not None, "no /status request was made")
    if status_req:
        check(status_req["headers"].get("T-Request-Id") == "PRID-1",
              f"/status must key off productRequestId via T-Request-Id: {status_req['headers']}")
        check("T-Session-Id" not in status_req["headers"],
              "/status must NOT carry T-Session-Id (that was the bug)")

    acct_req = next((r for r in jar.reqs if r["leaf"] == "account"), None)
    check(acct_req and acct_req["headers"].get("T-Session-Id") == "SID-1",
          f"/account must key off the session id via T-Session-Id: {acct_req and acct_req['headers']}")

    init = next(r for r in jar.reqs if r["leaf"] == "session")
    check(init["body"]["fingerprint"]["userAgent"] == MobileSession.TPAY_WEBVIEW_UA,
          "the fingerprint must carry the webview UA, not the native one")
    check("Mozilla/5.0" in init["headers"].get("User-Agent", ""),
          "tpay requests must go out with the webview User-Agent")
    print("  tpay: ACCOUNT_TPW sent, /status vs /account headers correct, webview UA")


# ---- the fixture must not drift from the capture ---------------------------

def test_fixture_matches_capture():
    """When the real capture is present, the fixture must still match its SHAPE."""
    capture = os.environ.get("TBANK_CAPTURE_TRAVEL",
                             os.path.expanduser("~/tbank-app/captures-flight-train.xml"))
    if not os.path.exists(capture):
        print(f"  (capture absent — fixture shape not re-verified: {capture})")
        return
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))
    import regen_travel as R

    parsed = R.items(capture)
    for name, index in R.ITEMS.items():
        tag = "request" if name.endswith("_request") else "response"
        try:
            live = json.loads(R.body_of(R.raw(parsed[index], tag)))
        except Exception as e:  # noqa: BLE001
            failures.append(f"fixture drift: cannot read capture item {index} "
                            f"for {name}: {e}")
            continue
        a, b = shape(FX[name]), shape(live)
        missing = sorted(set(b) - set(a))
        extra = sorted(set(a) - set(b))
        check(not missing,
              f"fixture drift: {name} is missing {missing[:4]} — "
              f"re-run tests/fixtures/regen_travel.py")
        check(not extra,
              f"fixture drift: {name} has stale {extra[:4]} — "
              f"re-run tests/fixtures/regen_travel.py")


def test_travel_pay_signature_is_reproduced():
    """The travel-webview x-api-signature, recovered from the payment-child-app JS
    (getApiSignature / httpIntegrityCheck) and now reproduced in Python.

    Two checks: a fixed synthetic vector pins the MESSAGE FORMAT so any drift in it
    (line order, the "POST" prefix, the urlencoded query, the operation) breaks the
    hash; and, when the real capture is present, the exact captured signature of the
    flight-pay POST (item 1109) is reproduced byte for byte — the non-circular proof,
    like tests/test_transfer.py does for /v1/pay."""
    from tbank_mcp.client import travel_api_signature, TRAVEL_SIG_HEADER
    import urllib.parse

    # (1) format stability — a fake session, a fixed body, a frozen expected hash.
    sid = "TESTsession.authenticon-testpod-xxxxx"
    body = '{"amount":100,"offerId":"u-1"}'
    sig = travel_api_signature(sid, "travel_pay", {}, body)
    check(sig == "HvdzHmUBGQJKpfP63P7yVeQrcD+gKFAiIy77CcoO5lc=",
          f"the signature message format drifted: {sig!r}")
    check(TRAVEL_SIG_HEADER == "X-Api-Signature",
          f"the signature header name changed: {TRAVEL_SIG_HEADER!r}")
    # spell the signed message out so a reader can see exactly what is covered.
    q = urllib.parse.urlencode({"context": "travel", "sessionId": sid})
    check("\n".join(["POST", "travel_pay", q, body]).count("\n") == 3,
          "the signed message must be the 4-line POST/operation/query/body form")

    # (2) real capture — reproduce the captured signature exactly.
    capture = os.environ.get("TBANK_CAPTURE_TRAVEL",
                             os.path.expanduser("~/tbank-app/captures-flight-train.xml"))
    if not os.path.exists(capture):
        print("  travel_pay signature: format pinned (capture absent — real match skipped)")
        return
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))
    import regen_travel as R
    req = R.raw(R.items(capture)[R.ITEMS["flight_pay_request"]], "request")
    head, _, real_body = req.partition(b"\r\n\r\n")
    hs = head.decode("latin1")
    path = hs.split("\r\n", 1)[0].split(" ")[1]
    key = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)["sessionId"][0]
    real_sig = next((l.split(":", 1)[1].strip() for l in hs.split("\r\n")
                     if l.lower().startswith("x-api-signature:")), None)
    got = travel_api_signature(key, "travel_pay", {}, real_body.decode("utf-8"))
    check(got == real_sig,
          f"the captured travel_pay signature was not reproduced:\n  got  {got}\n  real {real_sig}")

    # (3) the WHOLE request builder — feed the capture's own detach/trace/session ids
    # and cookie, then every header the builder sets must equal the capture exactly.
    def cap_hdr(n):
        return next((l.split(":", 1)[1].strip() for l in hs.split("\r\n")
                     if l.lower().startswith(n.lower() + ":")), None)
    from tbank_mcp.client import MobileSession
    sess = MobileSession("sid", "rt")
    url, built = sess._travel_pay_request(
        key, cap_hdr("Cookie"), real_body.decode("utf-8"),
        detach_key=cap_hdr("X-Detach-Key"),
        travel_session_id=cap_hdr("X-Travel-Session-Id"),
        trace_id=cap_hdr("X-Trace-Id"))
    check(url == "https://www.tbank.ru" + path,
          f"the built travel_pay URL diverged:\n  {url}\n  https://www.tbank.ru{path}")
    for h in ("X-Api-Signature", "X-Detach-Key", "X-Detach-Timeout",
              "X-Travel-Session-Id", "X-Trace-Id", "X-Travel-Context",
              "Content-Type", "Cookie"):
        check(built.get(h) == cap_hdr(h),
              f"built header {h} != capture:\n  built {built.get(h)!r}\n  cap   {cap_hdr(h)!r}")
    check("Authorization" not in built,
          "travel_pay must NOT carry a mobile Bearer — it is a web-cookie request")
    check(built["X-Travel-Context"] == "webview",
          f"travel_pay context must be webview: {built['X-Travel-Context']!r}")
    # (4) the session-link parse: check_auth (item 992) posts the travel sessionId
    # back to the opener; _parse_link_session must recover it, and it must be the
    # value used as the signing key upstream.
    mint_html = R.body_of(R.raw(R.items(capture)[992], "response")).decode("utf-8", "replace")
    parsed = MobileSession._parse_link_session(mint_html)
    check(parsed.get("sessionId") == key,
          f"the check_auth parse did not recover the travel sessionId: {parsed!r}")
    check(parsed.get("accessLevel") == "CLIENT",
          f"the minted travel session must be CLIENT level: {parsed!r}")
    # a page that is not the postMessage shape yields no session, not a guess.
    check(MobileSession._parse_link_session("<html>login required</html>") == {},
          "a non-session page must parse to empty, never a partial session")
    print("  travel_pay: signature AND the whole signed request reproduced byte-exact "
          "against the flight-pay capture; check_auth sessionId parse pinned too")


def test_flight_pay_without_sso_sends_nothing():
    """flight_pay signs with the travel web session, minted by the session-link bridge
    from the SSO login cookie. With no SSO_SESSION there is nothing to mint from, so
    both the bridge and flight_pay must refuse BEFORE any network call — nothing
    signed, nothing sent, nothing charged. A money call that quietly went out unsigned
    would be the worst outcome, so the refusal must precede even opening a jar."""
    from tbank_mcp.client import MobileSession, TbankApiError

    posted = []

    class Blocked:
        def post(self, *a, **k):
            posted.append(("post", a, k)); raise AssertionError("network!")
        def get(self, *a, **k):
            posted.append(("get", a, k)); raise AssertionError("network!")

    def no_jar():
        raise AssertionError("opened a jar before checking for an SSO session")

    s = MobileSession("sid", "rt")   # no sso_login_cookie, no travel_session_id
    s._http = Blocked()
    s._fresh_jar = no_jar
    try:
        s.flight_pay({"offerId": "u-1", "amount": 100})
        failures.append("flight_pay sent a payment with no SSO session")
    except TbankApiError as e:
        check(e.result_code == "NO_SSO_SESSION",
              f"flight_pay must refuse without SSO: {e.result_code}")
    check(not posted, f"flight_pay hit the network before it had a session: {posted}")

    try:
        s.travel_link_session()
        failures.append("travel_link_session pretended to work with no SSO cookie")
    except TbankApiError as e:
        check(e.result_code == "NO_SSO_SESSION",
              f"travel_link_session must refuse without SSO: {e.result_code}")
    print("  flight_pay: with no SSO_SESSION, bridge and pay both refuse before the wire")


def test_travel_link_bridge_drives_the_captured_legs():
    """Drive travel_link_session() through a recording jar and pin the three web legs.

    The bridge moves no money, so it is exercised live too — but a unit test keeps the
    request SHAPES from drifting: the authorize query (origin/appName/appVersion/theme/
    link_token) is exactly the captured flights webview's, the id.tbank.ru Location is
    FOLLOWED rather than reconstructed, and the sessionId is recovered from the
    check_auth postMessage. The SSO cookies must be seeded so they reach BOTH
    www.tbank.ru and id.tbank.ru."""
    import urllib.parse
    from tbank_mcp.client import (MobileSession, TbankApiError, TRAVEL_LINK_APP,
                                  TRAVEL_LINK_APP_VERSION, TRAVEL_LINK_ORIGIN)

    ID_URL = ("https://id.tbank.ru/auth/authorize?state=JWT&client_id=portal-api-link"
              "&code_challenge=CH&code_challenge_method=S256&auth_token=LINKTOKEN"
              "&redirect_uri=https%3A%2F%2Fwww.tbank.ru%2Fapi%2Fcommon%2Fv1%2F"
              "session%2Flink%2Fcheck_auth%2F")
    CHECK_URL = ("https://www.tbank.ru/api/common/v1/session/link/check_auth/"
                 "?code=c.CODE&state=JWT&session_state=SS")
    MINT_HTML = ('<html><script>window.parent.postMessage({"sessionId":'
                 '"TOKEN.authenticon-pod-abcde","accessLevel":"CLIENT",'
                 '"messageCode":"authComplete"}, \'*\')</script></html>')

    class Resp:
        def __init__(self, status=200, location=None, text=""):
            self.status_code = status
            self.headers = {"Location": location} if location else {}
            self.text = text

    class Cookies:
        def __init__(self):
            self.jar = {}

        def set(self, k, v, domain=None):
            self.jar[(k, domain)] = v

    class RecJar:
        def __init__(self, level="CLIENT"):
            self.gets = []
            self.cookies = Cookies()
            self.level = level

        def get(self, url, params=None, headers=None, timeout=None, allow_redirects=None):
            self.gets.append({"url": url, "params": params or {},
                              "redirects": allow_redirects})
            if "session/link/authorize" in url:
                return Resp(303, location=ID_URL)
            if url.startswith("https://id.tbank.ru/auth/authorize"):
                return Resp(303, location=CHECK_URL)
            if "session/link/check_auth" in url:
                return Resp(200, text=MINT_HTML.replace('"CLIENT"', f'"{self.level}"'))
            raise AssertionError("unexpected url " + url)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    s = MobileSession("sid", "rt")
    s.sso_login_cookie = "SSO_SESSION=abc; api_sso_id=def; sso_used=1"
    jar = RecJar()
    s._fresh_jar = lambda: jar
    s.travel_link_auth_token = lambda: "LINKTOKEN"

    sid = s.travel_link_session(force=True)
    check(sid == "TOKEN.authenticon-pod-abcde",
          f"the bridge must return the postMessage sessionId: {sid}")
    check(s.travel_session_id == sid and s.travel_session_at > 0,
          "the minted session must be cached with a timestamp")

    domains = {d for (_, d) in jar.cookies.jar}
    check(domains == {".tbank.ru"},
          f"SSO cookies must be seeded at .tbank.ru so id.tbank.ru gets them: {domains}")
    names = {k for (k, _) in jar.cookies.jar}
    check({"SSO_SESSION", "api_sso_id", "sso_used"} <= names,
          f"the SSO cookies must be on the jar: {names}")

    authorize = next(g for g in jar.gets if "session/link/authorize" in g["url"])
    check(authorize["params"] == {"origin": TRAVEL_LINK_ORIGIN, "appName": TRAVEL_LINK_APP,
                                  "appVersion": TRAVEL_LINK_APP_VERSION, "theme": "context",
                                  "link_token": "LINKTOKEN"},
          f"authorize query drifted from the capture: {authorize['params']}")
    check(authorize["redirects"] is False,
          "authorize must NOT follow redirects — its Location is the SSO url to read")
    idleg = next(g for g in jar.gets if g["url"].startswith("https://id.tbank.ru"))
    check(idleg["url"] == ID_URL,
          "the SSO leg must GET the exact Location the bank returned, not a reconstruction")
    # Match the check_auth ENDPOINT, not the substring: the id.tbank.ru url also
    # carries "check_auth" inside its redirect_uri, so a loose match grabs that leg.
    checkleg = next(g for g in jar.gets
                    if g["url"].startswith(
                        "https://www.tbank.ru/api/common/v1/session/link/check_auth"))
    check(checkleg["url"] == CHECK_URL, "check_auth must be the Location from the SSO leg")

    # A non-CLIENT session would sign a pay the gateway then rejects — refuse it.
    s2 = MobileSession("sid", "rt")
    s2.sso_login_cookie = "SSO_SESSION=abc"
    s2._fresh_jar = lambda: RecJar(level="ANONYMOUS")
    s2.travel_link_auth_token = lambda: "LINKTOKEN"
    try:
        s2.travel_link_session(force=True)
        failures.append("a non-CLIENT travel session was accepted")
    except TbankApiError as e:
        check(e.result_code == "TRAVEL_LINK_NOT_CLIENT",
              f"non-CLIENT must be refused by name: {e.result_code}")
    # The refusal is only half of it: the ANONYMOUS session must be left UN-cached, or
    # the next flight_pay within the window would reuse it and sign a doomed pay.
    check(s2.travel_session_id == "" and s2.travel_session_at == 0.0,
          "an ANONYMOUS session must NOT be cached — it would poison the next pay")

    # Capture cross-check: our authorize params == the real webview's, field for field.
    capture = os.environ.get("TBANK_CAPTURE_TRAVEL",
                             os.path.expanduser("~/tbank-app/captures-flight-train.xml"))
    if os.path.exists(capture):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))
        import regen_travel as R
        line = next(
            R.raw(it, "request").split(b"\r\n", 1)[0].decode("latin1")
            for it in R.items(capture)
            if b"session/link/authorize" in R.raw(it, "request").split(b"\r\n", 1)[0])
        cq = urllib.parse.parse_qs(urllib.parse.urlparse(line.split(" ")[1]).query)
        for k in ("origin", "appName", "appVersion", "theme"):
            check(cq[k][0] == authorize["params"][k],
                  f"authorize {k}: capture {cq[k][0]!r} != ours {authorize['params'][k]!r}")
        check("link_token" in cq, "the captured authorize must carry a link_token")
        print("  travel-link: bridge legs + authorize query pinned against the capture")
    else:
        print("  travel-link: bridge legs pinned (capture absent — query cross-check skipped)")


def test_flight_pay_with_preset_session_skips_the_bridge():
    """An explicit travel_session_id signs and posts directly — the bridge is only for
    when one is not supplied, and must not run when it is. This is also the override a
    caller uses to pin a known-good session."""
    from tbank_mcp.client import MobileSession

    class Resp:
        status_code = 200

        def json(self):
            return {"status": "Working", "detachKey": "ORDER-1", "payload": {}}

    posted = {}

    class Http:
        def post(self, url, data=None, headers=None, timeout=None):
            posted["url"] = url
            posted["headers"] = headers
            return Resp()

    s = MobileSession("sid", "rt")
    s._http = Http()
    s._fresh_jar = lambda: (_ for _ in ()).throw(AssertionError("the bridge ran!"))
    env = s.flight_pay({"offerId": "u-1", "amount": 100},
                       travel_session_id="WEBSID.authenticon-pod-x", cookie="ma_ss=1")
    check(env.get("detachKey") == "ORDER-1", f"the envelope must pass through: {env}")
    check("sessionId=WEBSID.authenticon-pod-x" in posted["url"],
          f"the preset session must key the signed request: {posted['url']}")
    check("X-Api-Signature" in posted["headers"] and "Authorization" not in posted["headers"],
          "a travel pay is signed and web-cookie, never a mobile Bearer")
    print("  flight_pay: an explicit travel session signs directly, no bridge call")


def test_flight_pay_mint_failure_never_reads_as_unknown():
    """A mint failure is ALWAYS pre-POST, so flight_book must read it as «not sent /
    nothing charged», never «unknown outcome». The mint's DEPENDENCIES raise codes the
    server list does not name literally — the CLIENT re-mint raises SessionExpired
    ('invalid_grant'), the lapsed-window token read raises INSUFFICIENT_PRIVILEGES /
    HTTP_401, a WAF interstitial raises HTTP_200 — so flight_pay must re-tag every
    mint-time failure under the TRAVEL_LINK prefix the handler routes to «not sent».
    Nothing may be POSTed. (Regression for the misclassification the review caught.)"""
    from tbank_mcp.client import (MobileSession, TbankApiError, SessionExpired,
                                  UnreadableResponse)

    def whitelisted(code):   # mirrors server._do_flight_book's not-sent predicate
        return code.startswith("TRAVEL_LINK") or code in ("NO_SSO_SESSION", "NO_LINK_TOKEN")

    posted = []

    class Blocked:
        def post(self, *a, **k):
            posted.append("POST")
            raise AssertionError("a payment POST fired on a mint failure")

    cases = [
        SessionExpired("invalid_grant", "токен истёк"),        # ensure_client_session -> refresh
        TbankApiError("INSUFFICIENT_PRIVILEGES", "нет прав"),  # link_token read, lapsed window
        SessionExpired("HTTP_401", "401"),                     # _status_error on the token read
        UnreadableResponse("HTTP_200", "мусор"),               # WAF interstitial, unparseable 200
        TbankApiError("NO_SSO_SESSION", "нет sso"),            # explicit pre-send, passes through
        TbankApiError("TRAVEL_LINK_NO_CODE", "нет кода"),      # explicit pre-send, passes through
    ]
    for exc in cases:
        s = MobileSession("sid", "rt")
        s._http = Blocked()
        s.travel_link_session = lambda e=exc: (_ for _ in ()).throw(e)
        try:
            s.flight_pay({"offerId": "u-1", "amount": 100})
            failures.append(f"flight_pay did not raise for {exc.result_code}")
        except TbankApiError as e:
            check(whitelisted(e.result_code),
                  f"mint failure {exc.result_code} escaped as {e.result_code} — "
                  "flight_book would read it as «money may have moved»")
        check(not posted, f"a POST fired on the {exc.result_code} mint failure: {posted}")
        posted.clear()
    print("  flight_pay: every mint failure reads «not sent», never «unknown», no POST")


def test_flight_pay_result_polls_the_web_session_not_mobile():
    """The pay POST and its result poll must run on the SAME web travel session — the
    gateway ties the in-flight payment to that session, not to any detach key. Polling
    on the mobile session (the first-live-attempt bug) asks the gateway about a payment
    that session never made → 400 → a false «исход неизвестен». Pin: the result GET
    carries context=travel + the web sessionId, X-Travel-Context: webview, NO Bearer;
    and refuses when there is no travel session to poll. Cross-checked against the
    captured poll's shape."""
    import urllib.parse
    from tbank_mcp.client import MobileSession, TbankApiError

    class Resp:
        status_code = 200

        def json(self):
            return {"status": "Working", "payload": {}}

    posted = {}

    class Http:
        def get(self, url, headers=None, timeout=None):
            posted["url"] = url
            posted["headers"] = headers or {}
            return Resp()

        def post(self, *a, **k):
            raise AssertionError("pay/result must be a GET, not a POST")

    s = MobileSession("sid", "rt")
    s.travel_session_id = "WEBSID.authenticon-pod-x"
    s.cookie_str = "__P__wuid=w; api_sso_id=a; sso_used=1"
    s._http = Http()
    env = s.flight_pay_result()
    check(env.get("status") == "Working", f"the envelope must pass through: {env}")
    check("/api/travel/flight/booking/pay/result" in posted["url"],
          f"wrong path: {posted['url']}")
    check("sessionId=WEBSID.authenticon-pod-x" in posted["url"]
          and "context=travel" in posted["url"],
          f"result must poll the WEB travel session: {posted['url']}")
    h = posted["headers"]
    check(h.get("X-Travel-Context") == "webview",
          f"result poll must be webview context, was {h.get('X-Travel-Context')!r}")
    check("Authorization" not in h,
          "result poll must NOT carry a mobile Bearer — it is a web-cookie request")
    check("X-Detach-Key" in h and "X-Trace-Id" in h and "Cookie" in h,
          "result poll carries the webview detach/trace nonces and the web cookie")

    # With no travel session there is nothing to poll — refuse, do not fall back to
    # the mobile session (that is exactly the bug this replaces).
    s2 = MobileSession("sid", "rt")
    try:
        s2.flight_pay_result()
        failures.append("flight_pay_result polled with no travel session")
    except TbankApiError as e:
        check(e.result_code == "NO_TRAVEL_SESSION",
              f"must refuse without a travel session: {e.result_code}")

    # Capture cross-check: the real poll uses exactly {context, sessionId}, webview, no Bearer.
    capture = os.environ.get("TBANK_CAPTURE_TRAVEL",
                             os.path.expanduser("~/tbank-app/captures-flight-train.xml"))
    if os.path.exists(capture):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))
        import regen_travel as R
        items = R.items(capture)
        idx = next(i for i, it in enumerate(items)
                   if b"booking/pay/result" in R.raw(it, "request").split(b"\r\n", 1)[0])
        head = R.raw(items[idx], "request").split(b"\r\n\r\n", 1)[0].decode("latin1")
        line = head.split("\r\n", 1)[0]
        q = urllib.parse.parse_qs(urllib.parse.urlparse(line.split(" ")[1]).query)
        check(sorted(q) == ["context", "sessionId"],
              f"real pay/result query keys drifted from the capture: {sorted(q)}")

        def has(name):
            return any(l.lower().startswith(name + ":") for l in head.split("\r\n")[1:])
        check(has("x-travel-context") and not has("authorization"),
              "the real poll is webview context with no Bearer — our build must match")
        print("  flight_pay_result: polls the web session (webview, no Bearer), pinned vs capture")
    else:
        print("  flight_pay_result: web-session poll pinned (capture absent — cross-check skipped)")


def test_pay_result_detail_reads_both_envelope_shapes():
    """A non-Ok pay/result must be diagnosable. _envelope yields two shapes: a JSON
    error (message in errorMessage/payload.message, and NO `text` key) and a non-JSON
    body (message in `text`). The detail extractor must read BOTH — the first version
    read only `text` and silently dropped every JSON error message, which is the
    common gateway shape."""
    from tbank_mcp.server import _pay_result_detail
    j = {"status": "Error", "http": 400, "payload": {"message": "Проверьте данные"}}
    check("Проверьте данные" in _pay_result_detail(j),
          f"must read payload.message from a JSON error: {_pay_result_detail(j)!r}")
    e = {"status": "Error", "http": 400, "errorMessage": "Field value is wrong"}
    check("Field value is wrong" in _pay_result_detail(e),
          f"must read errorMessage: {_pay_result_detail(e)!r}")
    t = {"status": "Unreadable", "http": 400, "text": "Bad Request"}
    check("Bad Request" in _pay_result_detail(t),
          f"must fall back to text for a non-JSON body: {_pay_result_detail(t)!r}")
    check(_pay_result_detail({"status": "X", "http": 400}) == "",
          "no message anywhere → empty string, not a crash")
    print("  pay/result detail: reads JSON message AND non-JSON text, empty when absent")


def test_travel_link_session_caches_within_window():
    """The mint is cached for the ~11-min portal window: a second call without force,
    inside TRAVEL_PORTAL_TTL, must REUSE it and not re-run the bridge (re-minting burns
    the single-use link_token and adds a round-trip). force=True, or a session aged past
    the window, must re-mint. This is the DEFAULT flight_pay path — no preset id, a warm
    cache — and nothing else exercises it."""
    from tbank_mcp.client import MobileSession, TRAVEL_PORTAL_TTL

    class Resp:
        def __init__(self, status=200, location=None, text=""):
            self.status_code = status
            self.headers = {"Location": location} if location else {}
            self.text = text

    class Cookies:
        def set(self, *a, **k):
            pass

    ID_URL = "https://id.tbank.ru/auth/authorize?state=JWT"
    CHECK_URL = "https://www.tbank.ru/api/common/v1/session/link/check_auth/?code=c.C"
    HTML = ('<html><script>window.parent.postMessage({"sessionId":"S.authenticon-p-x",'
            '"accessLevel":"CLIENT"}, \'*\')</script></html>')

    class Jar:
        cookies = Cookies()

        def get(self, url, **k):
            if "session/link/authorize" in url:
                return Resp(303, location=ID_URL)
            if url.startswith("https://id.tbank.ru"):
                return Resp(303, location=CHECK_URL)
            return Resp(200, text=HTML)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    mints = {"jars": 0, "tokens": 0}
    s = MobileSession("sid", "rt")
    s.sso_login_cookie = "SSO_SESSION=abc"
    s._fresh_jar = lambda: (mints.__setitem__("jars", mints["jars"] + 1), Jar())[1]
    s.travel_link_auth_token = lambda: (mints.__setitem__("tokens", mints["tokens"] + 1),
                                        "LINKTOKEN")[1]

    sid1 = s.travel_link_session()
    check(mints["jars"] == 1 and mints["tokens"] == 1, "the first call must mint once")
    sid2 = s.travel_link_session()
    check(sid2 == sid1 and mints["jars"] == 1 and mints["tokens"] == 1,
          f"a warm call must reuse the cache, not re-mint: jars={mints['jars']}")
    s.travel_link_session(force=True)
    check(mints["jars"] == 2, "force=True must re-mint regardless of the window")
    s.travel_session_at -= (TRAVEL_PORTAL_TTL + 1)
    s.travel_link_session()
    check(mints["jars"] == 3, "a session older than the window must re-mint")
    print("  travel-link: cache reused within the window; force and age both re-mint")


def test_travel_link_auth_token_refuses_empty():
    """An empty/absent token from the bank must raise NO_LINK_TOKEN — not return "" and
    let the bridge build an authorize with no link_token (which would mint the wrong
    thing or fail opaquely). This is the one leg the bridge tests stub, so pin it here."""
    from tbank_mcp.client import MobileSession, TbankApiError

    s = MobileSession("sid", "rt")
    s.ensure_client_session = lambda: "CLIENT"
    s._call_read = lambda *a, **k: {"token": ""}
    try:
        s.travel_link_auth_token()
        failures.append("an empty link_token was accepted")
    except TbankApiError as e:
        check(e.result_code == "NO_LINK_TOKEN",
              f"an empty token must raise NO_LINK_TOKEN: {e.result_code}")
    s._call_read = lambda *a, **k: {"token": "ABC123"}
    check(s.travel_link_auth_token() == "ABC123", "a present token must be returned")
    print("  travel-link: empty link_token refused, present token returned")


def test_the_flight_pay_envelope_reports_the_shape_it_got():
    """_envelope carries the state that lives OUTSIDE `payload` for flight pay/result,
    and must never raise on a non-2xx — for those calls a 400 means «nothing in
    flight», not «broken». A mutation to its branches broke no test, so pin all three:
    a dict body (http filled, not overwritten), an unreadable body, and a non-dict
    JSON body."""
    class Resp:
        def __init__(self, body, status=200, text="", raise_json=False):
            self._body, self.status_code, self.text = body, status, text
            self._raise = raise_json
        def json(self):
            if self._raise:
                raise ValueError("no json")
            return self._body

    # (a) dict body: status_code fills `http` via setdefault…
    d = MobileSession._envelope(Resp({"status": "WaitingPayment"}, status=200))
    check(d["status"] == "WaitingPayment" and d["http"] == 200,
          f"a dict envelope must keep its fields and gain http: {d}")
    # …but an http the payload already carried must NOT be overwritten.
    d2 = MobileSession._envelope(Resp({"status": "X", "http": 418}, status=200))
    check(d2["http"] == 418, f"setdefault must not clobber an existing http: {d2}")

    # (b) unreadable body on a non-2xx: no raise, status=Unreadable, text kept.
    u = MobileSession._envelope(Resp(None, status=400, text="upstream 400 boom",
                                     raise_json=True))
    check(u["status"] == "Unreadable" and u["http"] == 400,
          f"an unreadable body must be reported, not raised: {u}")
    check("boom" in u["text"], f"the body excerpt must survive for diagnosis: {u}")

    # (c) JSON that is not a dict (a bare list) → Unexpected, data preserved.
    x = MobileSession._envelope(Resp([1, 2, 3], status=200))
    check(x["status"] == "Unexpected" and x["data"] == [1, 2, 3],
          f"a non-dict JSON body must be flagged, its data kept: {x}")
    print("  _envelope: dict/unreadable/non-dict shapes all reported, never raised")


def main():
    for fn in (test_rail_order_body, test_rail_seat_order_is_passenger_order,
               test_rail_taken_seat_is_named, test_refund_key_case_differs,
               test_flight_pay_body, test_flight_bonus_card_both_ways,
               test_flight_seats_refuse_connections,
               test_own_passenger_is_the_owner_not_a_relative,
               test_a_minor_is_refused_on_the_booking_path,
               test_tpay_init_shape,
               test_tpay_flow_sends_account_step_and_correct_headers,
               test_travel_pay_signature_is_reproduced,
               test_flight_pay_without_sso_sends_nothing,
               test_travel_link_bridge_drives_the_captured_legs,
               test_flight_pay_with_preset_session_skips_the_bridge,
               test_flight_pay_result_polls_the_web_session_not_mobile,
               test_pay_result_detail_reads_both_envelope_shapes,
               test_flight_pay_mint_failure_never_reads_as_unknown,
               test_travel_link_session_caches_within_window,
               test_travel_link_auth_token_refuses_empty,
               test_the_flight_pay_envelope_reports_the_shape_it_got,
               test_fixture_matches_capture):
        fn()
    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print("ok — travel booking bodies match the captured app traffic")
    return 0


if __name__ == "__main__":
    sys.exit(main())
