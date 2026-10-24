# notify-plus - add-on for Kiln OS's notify plugin (kiln-notify)
#
# kiln-notify sends every message through KilnNotify.send(), which hands it
# to Apprise. On Discord that gives plain text with the photo attached.
# When Moonraker starts, this component wraps that method:
#   - Discord webhook URLs get Discord embeds laid out like
#     FlashForgeUI-Electron's (status, temps, progress bar, time, ETA,
#     layer, IFS slot, file, photo inside the card). If Discord refuses
#     a card, that URL gets kiln-notify's plain text as before.
#     (switch: SAVE_VARIABLE VARIABLE=notify_discord VALUE=0)
#   - every other URL goes through kiln-notify unchanged.
# It also adds what FLSUN_notify / U1_notify have and kiln-notify doesn't:
#   - "Printer on" when Klipper is ready within 5 minutes of a boot
#     (switch: SAVE_VARIABLE VARIABLE=notify_on VALUE=0)
#   - _NOTIFY / NOTIFY TYPE= muting (plugin.cfg; notify_<type> = 0)
#
# Kiln OS plugin (plugin API 1.3): Moonraker loads this file from the
# plugin's moonraker/components because moonraker.conf has [notify_plus].
# kiln-notify must be enabled first (plugin.json "requires").
#
# Copyright (C) 2026 DIEHARDave
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import asyncio
import datetime
import json
import logging
import re
import uuid

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper

DISCORD_RE = re.compile(
    r"^(?:https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/"
    r"|discord://(?:[^@/]*@)?)(?P<id>\d+)/(?P<token>[\w-]+)"
)
# kiln-notify's message texts (its TEXT and the strings in its event
# handlers), without the name it puts in front. Anything else is a
# message of the user's own (NOTIFY / _NOTIFY) or this component's.
EVENT_RES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("started", re.compile(r"^Started ")),
    ("complete", re.compile(r"^Finished ")),
    ("error", re.compile(r"^Print failed: ")),
    ("cancelled", re.compile(r"^Cancelled ")),
    ("paused", re.compile(r"^Paused ")),
    ("resumed", re.compile(r"^Resumed ")),
    ("progress", re.compile(r"^\d+% ")),
    ("refill", re.compile(r"^Auto-refill: ")),
    ("interrupted", re.compile(r"^A print was interrupted")),
    ("shutdown", re.compile(r"^Klipper shut down")),
)
# The card shows these events' details itself: no description line
NO_DESCRIPTION = ("started", "resumed", "progress")
# Seconds to wait before each attempt to reach Discord
RETRY_DELAYS = (0., 10., 20., 30.)
# "Printer on" only this soon after a boot, not on later Klipper restarts
BOOT_WINDOW = 300.
# FlashForgeUI-Electron's status labels and embed colors
STATUS_LABELS = {
    "Ready": "✅ Ready",
    "Printing": "🖨️ Printing",
    "Paused": "⏸️ Paused",
    "Completed": "✅ Completed",
    "Error": "❌ Error",
    "Cancelled": "🚫 Cancelled",
}
STATUS_COLORS = {"Printing": 0x00ff00, "Ready": 0x3498db, "Paused": 0xf39c12,
                 "Error": 0xe74c3c}
EVENT_STATUS = {
    "started": "Printing",
    "resumed": "Printing",
    "progress": "Printing",
    "paused": "Paused",
    "error": "Error",
    "cancelled": "Cancelled",
    "shutdown": "Error",
}
KLIPPER_STATUS = {
    "printing": "Printing",
    "paused": "Paused",
    "error": "Error",
}


def classify(message: str) -> str:
    for event, pattern in EVENT_RES:
        if pattern.match(message):
            return event
    return "custom"


class NotifyPlus:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.kn: Any = None
        self.orig_send: Optional[Callable] = None
        self.on_sent = False
        self.server.register_remote_method(
            "notify_plus_status", self._remote_status)
        self.server.register_event_handler(
            "server:klippy_ready", self._on_ready)

    async def component_init(self) -> None:
        self.kn = self.server.lookup_component("kiln_notify", None)
        if self.kn is None:
            logging.info("notify_plus: kiln-notify isn't loaded "
                         "(ENABLE_PLUGIN NAME=notify)")
            return
        self.orig_send = self.kn.send
        # kiln-notify calls self.send(...): the instance attribute wins
        self.kn.send = self._send
        logging.info("notify_plus: wrapped kiln-notify")

    # Sending
    async def _send(self, message: str, photo: Optional[bool] = None,
                    urls: Optional[List[str]] = None) -> bool:
        assert self.orig_send is not None
        urls = self.kn.settings["urls"] if urls is None else urls
        hooks = [(u, DISCORD_RE.match(u)) for u in urls]
        if not any(m for _, m in hooks):
            return await self.orig_send(message, photo, urls)
        status = await self._query()
        if not _switch(status, "discord"):
            return await self.orig_send(message, photo, urls)
        others = [u for u, m in hooks if m is None]
        ok = True
        if others:
            ok = await self.orig_send(message, photo, others)
        if photo is None:
            photo = self.kn.settings["photo"]
        for url, m in hooks:
            if m is None:
                continue
            webhook = "https://discord.com/api/webhooks/%s/%s" % (
                m.group("id"), m.group("token"))
            try:
                embed, image = await self._build_embed(message, photo,
                                                       status)
                await self._post_discord(webhook, embed, image)
            except Exception:
                logging.exception(
                    "notify_plus: Discord embed failed, sending plain text")
                ok = await self.orig_send(message, photo, [url]) and ok
        return ok

    async def _query(self) -> Dict[str, Any]:
        kapis = self.server.lookup_component("klippy_apis")
        try:
            return await kapis.query_objects({
                "print_stats": None,
                "display_status": ["progress"],
                "virtual_sdcard": ["progress"],
                "extruder": ["temperature", "target"],
                "heater_bed": ["temperature", "target"],
                "gcode_move": ["gcode_position"],
                "ifs": ["channel", "colors"],
                "save_variables": ["variables"],
            }, default={})
        except Exception:
            # Klipper shut down or not connected: a card without its data
            return {}

    async def _build_embed(self, message: str, photo: bool,
                           status: Dict[str, Any]):
        stats = dict(status.get("print_stats") or {})
        event = classify(message)
        if event == "complete":
            embed = self._complete_embed(stats)
        else:
            text = "" if event in NO_DESCRIPTION else message
            embed = self._status_embed(event, stats, status, text)
        image = await self._snapshot() if photo else None
        if image is not None:
            embed["image"] = {"url": "attachment://snapshot.jpg"}
        return embed, image

    # Right after boot the printer's clock may still be wrong, so HTTPS
    # fails; Discord can also have a bad moment. Both are retried for
    # about a minute. Discord refusing the card (4xx) isn't.
    async def _post_discord(self, webhook: str, embed: Dict[str, Any],
                            photo: Optional[bytes]) -> None:
        client = self.server.lookup_component("http_client")
        resp = None
        for delay in RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            # Set per attempt, so a retry after the clock syncs gets the
            # right time
            embed["timestamp"] = datetime.datetime.now(
                datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            if photo is None:
                body: Any = json.dumps({"embeds": [embed]})
                headers = {"Content-Type": "application/json"}
            else:
                body, headers = _multipart({"embeds": [embed]}, photo)
            resp = await client.request(
                "POST", webhook, body, headers, request_timeout=15.)
            if resp.status_code in (200, 204):
                return
            if resp.status_code < 500:
                break
            logging.info("notify_plus: Discord unreachable (%s), retrying",
                         resp.status_code)
        raise RuntimeError("Discord returned %s: %s" % (
            resp.status_code if resp else "?",
            resp.text[:200] if resp else ""))

    def _printer_name(self) -> str:
        return self.kn.settings.get("name") or "AD5X"

    def _status_embed(self, event: str, stats: Dict[str, Any],
                      status: Dict[str, Any], message: str) -> Dict[str, Any]:
        label = EVENT_STATUS.get(event) or KLIPPER_STATUS.get(
            stats.get("state", ""), "Ready")
        fields: List[Dict[str, Any]] = [{
            "name": "Status",
            "value": STATUS_LABELS.get(label, label),
            "inline": True,
        }]
        for key, title in (("extruder", "Extruder Temp"),
                           ("heater_bed", "Bed Temp")):
            heater = status.get(key)
            if heater:
                fields.append({
                    "name": title,
                    "value": "%.2f°C / %.2f°C" % (
                        heater.get("temperature", 0.),
                        heater.get("target", 0.)),
                    "inline": True,
                })
        if label in ("Printing", "Paused") and stats.get("filename"):
            fields.extend(self._job_fields(stats, status))
        embed: Dict[str, Any] = {
            "title": "🖨️ %s" % (self._printer_name(),),
            "color": STATUS_COLORS.get(label, 0x95a5a6),
            "fields": fields,
        }
        if message:
            embed["description"] = message[:4000]
        return embed

    def _job_fields(self, stats: Dict[str, Any],
                    status: Dict[str, Any]) -> List[Dict[str, Any]]:
        filename = stats.get("filename", "")
        meta = self._metadata(filename)
        # M73 progress from the slicer is time based; the file position is
        # the fallback
        progress = float(
            (status.get("display_status") or {}).get("progress") or
            (status.get("virtual_sdcard") or {}).get("progress") or 0.)
        progress = min(max(progress, 0.), 1.)
        filled = int(progress * 100 // 10)
        fields = [{
            "name": "Progress",
            "value": "%s %d%%" % ("█" * filled + "░" * (10 - filled),
                                  round(progress * 100)),
            "inline": False,
        }]
        elapsed = float(stats.get("print_duration") or 0.)
        fields.append({"name": "Print Time", "value": _duration(elapsed),
                       "inline": True})
        estimate = float(meta.get("estimated_time") or 0.)
        remaining = None
        if estimate > 0:
            remaining = estimate * (1. - progress)
        elif progress > .01:
            remaining = elapsed / progress - elapsed
        if remaining is not None:
            # Discord shows <t:...> in each reader's own time zone
            eta = int(datetime.datetime.now(
                datetime.timezone.utc).timestamp() + remaining)
            fields.append({"name": "ETA", "value": "<t:%d:t>" % (eta,),
                           "inline": True})
        layer = _layer(stats, status, meta)
        if layer is not None:
            fields.append({"name": "Layer", "value": "%d / %d" % layer,
                           "inline": True})
        slot = _ifs_slot(status)
        if slot:
            fields.append({"name": "Filament", "value": slot,
                           "inline": True})
        fields.append({"name": "File", "value": filename, "inline": False})
        return fields

    def _complete_embed(self, stats: Dict[str, Any]) -> Dict[str, Any]:
        duration = float(stats.get("total_duration") or 0.)
        return {
            "title": "✅ Print Complete!",
            "color": 0x00ff00,
            "fields": [
                {"name": "File", "value": stats.get("filename") or "Unknown",
                 "inline": False},
                {"name": "Total Time",
                 "value": _duration(duration) if duration else "Unknown",
                 "inline": True},
            ],
            "footer": {"text": self._printer_name()},
        }

    def _metadata(self, filename: str) -> Dict[str, Any]:
        if not filename:
            return {}
        try:
            fm = self.server.lookup_component("file_manager")
            return fm.get_file_metadata(filename) or {}
        except Exception:
            return {}

    async def _snapshot(self) -> Optional[bytes]:
        client = self.server.lookup_component("http_client")
        url = self.kn.snapshot_url
        try:
            resp = await client.request("GET", url, request_timeout=8.,
                                        enable_cache=False)
        except Exception:
            return None
        if resp.has_error() or resp.status_code != 200 or not resp.content:
            logging.info("notify_plus: no snapshot from %s", url)
            return None
        return resp.content

    # Printer on
    async def _on_ready(self) -> None:
        if self.kn is None or self.on_sent or not self.kn.settings["urls"]:
            return
        self.on_sent = True
        try:
            with open("/proc/uptime") as f:
                uptime = float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            return
        if uptime > BOOT_WINDOW:
            return
        if _switch(await self._query(), "on"):
            await self.kn.send("Printer on")

    # Remote method (NOTIFY_PLUS_STATUS)
    async def _remote_status(self) -> None:
        if self.kn is None:
            msg = "kiln-notify isn't loaded (ENABLE_PLUGIN NAME=notify)"
        else:
            urls = self.kn.settings["urls"]
            n = len([u for u in urls if DISCORD_RE.match(u)])
            if not urls:
                msg = "no URL set (NOTIFY_SETUP URL=...)"
            elif n and not _switch(await self._query(), "discord"):
                msg = ("Discord cards off (SAVE_VARIABLE "
                       "VARIABLE=notify_discord VALUE=1 turns them on)")
            elif n:
                msg = "Discord embeds on for %d of %d URL(s)" % (n, len(urls))
            else:
                msg = "no Discord webhook URL, messages are sent as usual"
        self.server.send_event("server:gcode_response",
                               "// notify-plus: " + msg)


def _switch(status: Dict[str, Any], name: str) -> bool:
    """zMod-style switch notify_<name> in save_variables: on unless 0."""
    variables = (status.get("save_variables") or {}).get("variables") or {}
    return str(variables.get("notify_" + name, 1)) not in ("0", "False")


def _layer(stats: Dict[str, Any], status: Dict[str, Any],
           meta: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """print_stats.info has the layer when the slicer emits
    SET_PRINT_STATS_INFO; otherwise from the nozzle height and the file's
    layer height."""
    info = stats.get("info") or {}
    if info.get("current_layer") and info.get("total_layer"):
        return int(info["current_layer"]), int(info["total_layer"])
    total = int(meta.get("layer_count") or 0)
    height = float(meta.get("layer_height") or 0.)
    first = float(meta.get("first_layer_height") or height)
    pos = (status.get("gcode_move") or {}).get("gcode_position") or []
    if total <= 0 or height <= 0. or len(pos) < 3:
        return None
    layer = int(round((float(pos[2]) - first) / height)) + 1
    return min(max(layer, 1), total), total


def _ifs_slot(status: Dict[str, Any]) -> str:
    """'Slot 2 (#E53935)' for the IFS lane in the nozzle, '' without one."""
    ifs = status.get("ifs") or {}
    chan = ifs.get("channel") or 0
    if not chan:
        return ""
    colors = ifs.get("colors") or []
    color = colors[chan - 1] if 0 < chan <= len(colors) else None
    return "Slot %d%s" % (chan, " (%s)" % color if color else "")


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    return "%dh %dm" % (seconds // 3600, seconds % 3600 // 60)


def _multipart(payload: Dict[str, Any], photo: bytes):
    boundary = uuid.uuid4().hex
    body = b"".join([
        b"--%s\r\n" % boundary.encode(),
        b'Content-Disposition: form-data; name="payload_json"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps(payload).encode(), b"\r\n",
        b"--%s\r\n" % boundary.encode(),
        b'Content-Disposition: form-data; name="files[0]"; '
        b'filename="snapshot.jpg"\r\n',
        b"Content-Type: image/jpeg\r\n\r\n",
        photo, b"\r\n",
        b"--%s--\r\n" % boundary.encode(),
    ])
    headers = {"Content-Type": "multipart/form-data; boundary=%s" % boundary}
    return body, headers


def load_component(config: ConfigHelper) -> NotifyPlus:
    return NotifyPlus(config)
