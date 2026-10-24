# Offline test for notify-plus: stubs Moonraker and kiln-notify, checks
# which URLs get Discord cards, the card for each kiln-notify message,
# the plain-text fallback and the switches. No network.
#   python3 test_notify_plus.py
import asyncio
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "notify_plus", os.path.join(HERE, "moonraker", "components",
                                "notify_plus.py"))
np = importlib.util.module_from_spec(spec)
spec.loader.exec_module(np)

WEBHOOK = "https://discord.com/api/webhooks/123456789/abc-DEF_1"
STATUS = {
    "print_stats": {"state": "printing", "filename": "calicat.gcode",
                    "print_duration": 600., "total_duration": 3720.,
                    "message": "", "info": {"current_layer": 31,
                                            "total_layer": 175}},
    "display_status": {"progress": .36},
    "extruder": {"temperature": 220.1, "target": 220.},
    "heater_bed": {"temperature": 60., "target": 60.},
    "gcode_move": {"gcode_position": [0, 0, 6.2, 0]},
    "ifs": {"channel": 2, "colors": [None, "#E53935", None, None]},
    "save_variables": {"variables": {}},
}


class Resp:
    def __init__(self, code, content=b""):
        self.status_code = code
        self.content = content
        self.text = content.decode(errors="replace")

    def has_error(self):
        return self.status_code >= 400


class Http:
    def __init__(self):
        self.posts = []
        self.code = 204

    async def request(self, method, url, body=None, headers=None, **kw):
        if method == "GET":
            return Resp(200, b"\xff\xd8JPEG")
        self.posts.append((url, body, headers))
        return Resp(self.code, b"{}")


class Kapis:
    def __init__(self):
        self.status = json.loads(json.dumps(STATUS))

    async def query_objects(self, objs, default=None):
        return self.status


class FileManager:
    def get_file_metadata(self, name):
        return {"estimated_time": 1800., "layer_height": .2,
                "first_layer_height": .2, "layer_count": 175}


class KilnNotify:
    def __init__(self, urls):
        self.settings = {"urls": urls, "name": "AD5X-2", "photo": True}
        self.snapshot_url = "http://127.0.0.1/webcam/?action=snapshot"
        self.plain = []

    async def send(self, message, photo=None, urls=None):
        self.plain.append((message, list(urls or self.settings["urls"])))
        return True


class Server:
    def __init__(self, kn):
        self.comps = {"kiln_notify": kn, "http_client": Http(),
                      "klippy_apis": Kapis(), "file_manager": FileManager()}
        self.events = []

    def lookup_component(self, name, default=None):
        return self.comps.get(name, default)

    def register_remote_method(self, *a):
        pass

    def register_event_handler(self, *a):
        pass

    def send_event(self, *a):
        self.events.append(a)


class Config:
    def __init__(self, server):
        self.server = server

    def get_server(self):
        return self.server


def setup(urls):
    kn = KilnNotify(urls)
    server = Server(kn)
    plus = np.load_component(Config(server))
    asyncio.run(plus.component_init())
    return plus, kn, server


def card(post):
    url, body, headers = post
    if isinstance(body, bytes):
        start = body.index(b"\r\n\r\n") + 4
        body = body[start:body.index(b"\r\n--", start)].decode()
    return url, json.loads(body)["embeds"][0]


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        check.failed = True


check.failed = False

# Classification of every kiln-notify text
for text, event in (("Started calicat.gcode", "started"),
                    ("Finished calicat.gcode (1h 02m)", "complete"),
                    ("Print failed: calicat.gcode. Move out of range",
                     "error"),
                    ("Cancelled calicat.gcode", "cancelled"),
                    ("Paused calicat.gcode. Filament runout", "paused"),
                    ("Resumed calicat.gcode", "resumed"),
                    ("25% calicat.gcode", "progress"),
                    ("Auto-refill: slot 2 -> slot 3", "refill"),
                    ("A print was interrupted (power cut?): a.gcode.",
                     "interrupted"),
                    ("Klipper shut down (error); check the printer.",
                     "shutdown"),
                    ("Bed is hot", "custom")):
    check(np.classify(text) == event, "classify %r -> %s" % (text, event))

# Discord + Telegram: card to Discord, plain text to Telegram only
plus, kn, server = setup([WEBHOOK, "tgram://tok/123"])
http = server.comps["http_client"]
asyncio.run(kn.send("25% calicat.gcode"))
check(kn.plain == [("25% calicat.gcode", ["tgram://tok/123"])],
      "non-Discord URL goes through kiln-notify")
url, e = card(http.posts[0])
names = [f["name"] for f in e["fields"]]
check(url == WEBHOOK, "posts to the webhook")
check(e["title"] == "🖨️ AD5X-2", "title is kiln-notify's NAME")
check("description" not in e, "progress card has no description")
check(names[:3] == ["Status", "Extruder Temp", "Bed Temp"] and
      "Progress" in names and "ETA" in names, "status, temps, progress, ETA")
check({"name": "Layer", "value": "31 / 175", "inline": True} in e["fields"],
      "layer from print_stats.info")
check({"name": "Filament", "value": "Slot 2 (#E53935)", "inline": True}
      in e["fields"], "IFS slot field")
check(e.get("image") == {"url": "attachment://snapshot.jpg"},
      "photo inside the card")

# Finished: the complete card
http.posts.clear()
asyncio.run(kn.send("Finished calicat.gcode (1h 02m)"))
url, e = card(http.posts[0])
check(e["title"] == "✅ Print Complete!" and e["footer"]["text"] == "AD5X-2",
      "complete card")

# Paused: reason as description, paused color
http.posts.clear()
asyncio.run(kn.send("Paused calicat.gcode. Filament runout", photo=False))
url, e = card(http.posts[0])
check(e["description"] == "Paused calicat.gcode. Filament runout" and
      e["color"] == 0xf39c12 and "image" not in e,
      "paused card: reason, color, no photo with photo=False")

# Discord refuses the card: plain text to that URL
http.posts.clear()
http.code = 400
kn.plain.clear()
asyncio.run(kn.send("Bed is hot"))
check(kn.plain == [("Bed is hot", ["tgram://tok/123"]),
                   ("Bed is hot", [WEBHOOK])],
      "400 from Discord falls back to plain text for that URL")
http.code = 204

# notify_discord = 0: everything plain
server.comps["klippy_apis"].status["save_variables"]["variables"] = {
    "notify_discord": 0}
http.posts.clear()
kn.plain.clear()
asyncio.run(kn.send("Bed is hot"))
check(not http.posts and kn.plain == [("Bed is hot",
                                       [WEBHOOK, "tgram://tok/123"])],
      "notify_discord=0 sends plain text")

# No Discord URL: untouched
plus, kn, server = setup(["ntfy://ntfy.sh/topic"])
asyncio.run(kn.send("Started a.gcode"))
check(kn.plain == [("Started a.gcode", ["ntfy://ntfy.sh/topic"])] and
      not server.comps["http_client"].posts, "no Discord URL: untouched")

# Printer on (uptime is the test machine's, so only the switch is tested)
plus, kn, server = setup([WEBHOOK])
server.comps["klippy_apis"].status["save_variables"]["variables"] = {
    "notify_on": 0}
asyncio.run(plus._on_ready())
check(not server.comps["http_client"].posts, "notify_on=0: no Printer on")

sys.exit(1 if check.failed else 0)
