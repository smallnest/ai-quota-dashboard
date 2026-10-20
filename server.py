from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
import argparse
import asyncio
import json
import mimetypes
import os
import sys
import threading
import time


ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "quota.json"
MAX_BODY_BYTES = 16 * 1024
DEFAULT_PORT = int(os.environ.get("PORT") or 8765)
DEFAULT_BLE_ENABLED = os.environ.get("AUTO_BLE", "").lower() in {"1", "true", "yes", "on"}

BLE_NAME = os.environ.get("BLE_NAME", "Wio AI Quota")
SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
BLE_CHUNK_SIZE = 100

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}

ble_worker = None


def read_quota():
    with DATA_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_quota(payload):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def clamp_percent(value):
    try:
        number = round(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, number))


def compact_page(page):
    # One device page: headline value (t/v/l) plus up to four label/value
    # rows (r). b is an optional 0-100 bar; -1 hides it.
    rows = [{"n": str(r[0]), "v": str(r[1])} for r in (page.get("r") or [])[:4]]
    while len(rows) < 4:
        rows.append({"n": "", "v": ""})
    bar = page.get("b")
    return {
        "t": str(page.get("t", "--"))[:15],
        "v": str(page.get("v", "--")),
        "l": str(page.get("l", "--"))[:15],
        "b": clamp_percent(bar) if isinstance(bar, (int, float)) and bar >= 0 else -1,
        "r": rows,
    }


def compact_for_device(quota):
    magpie = quota.get("magpie") or {}
    footer = magpie.get("footer") or {}
    pages = magpie.get("pages") or []
    return {
        "u": str(quota.get("updatedAt") or footer.get("time") or ""),
        "f": {
            "c": str(footer.get("brand", "Magpie")),
            "k": str(footer.get("tokens", "--")),
            "t": str(footer.get("time") or quota.get("updatedAt") or "--:--"),
        },
        # Exactly four pages keep the payload shape fixed, so the firmware can
        # index p[0..3] without a page-count negotiation.
        "p": [compact_page(p) for p in (pages + [{}] * 4)[:4]],
    }


def device_payload_from_quota(quota):
    return json.dumps(compact_for_device(quota), ensure_ascii=False, separators=(",", ":")) + "\n"


class BleAutoSync:
    def __init__(self, device_name=BLE_NAME, interval=10):
        self.device_name = device_name
        self.interval = interval
        self.enabled = False
        self.status = "stopped"
        self.last_error = ""
        self.last_sent_at = ""
        self.last_devices = []
        self.sync_requested = False
        self.thread = None
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()

    def snapshot(self):
        return {
            "enabled": self.enabled,
            "status": self.status,
            "deviceName": self.device_name,
            "lastError": self.last_error,
            "lastSentAt": self.last_sent_at,
            "lastDevices": self.last_devices,
        }

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.enabled = True
        self.stop_event.clear()
        self.wake_event.clear()
        self.thread = threading.Thread(target=self._run_loop, name="ble-auto-sync", daemon=True)
        self.thread.start()

    def stop(self):
        self.enabled = False
        self.stop_event.set()
        self.wake_event.set()

    def trigger_sync(self):
        self.sync_requested = True
        self.wake_event.set()

    def _run_loop(self):
        try:
            import bleak  # noqa: F401
        except Exception as error:
            self.status = "bleak_missing"
            self.last_error = f"Python package 'bleak' is required for server-side BLE: {error}"
            print(f"[BLE] {self.last_error}")
            return

        while not self.stop_event.is_set():
            self.sync_requested = False
            try:
                quota = read_quota()
                payload = device_payload_from_quota(quota)
                self.status = "syncing"
                asyncio.run(self._send_payload(payload))
                self.last_error = ""
                self.last_sent_at = time.strftime("%H:%M:%S")
                self.status = "idle"
                print(f"[BLE] synced {len(payload.encode('utf-8'))} bytes at {self.last_sent_at}")
            except Exception as error:
                self.status = "error"
                self.last_error = str(error)
                print(f"[BLE] sync failed: {self.last_error}")

            # Wait for the interval, but wake immediately if a sync was triggered
            # or stop was requested. wake_event is the only thing that interrupts
            # the wait; stop_event controls loop termination.
            if not self.sync_requested:
                self.wake_event.wait(self.interval)
            self.wake_event.clear()

    async def _send_payload(self, payload):
        from bleak import BleakClient, BleakScanner

        print(f"[BLE] scanning for {self.device_name} by service UUID ...")
        devices = await BleakScanner.discover(timeout=6.0, service_uuids=[SERVICE_UUID])
        if not devices:
            print("[BLE] service UUID scan found no devices, scanning all BLE devices ...")
            devices = await BleakScanner.discover(timeout=8.0)

        self.last_devices = [
            {"name": item.name or "", "address": item.address}
            for item in devices[:20]
        ]
        device = next((item for item in devices if item.name and self.device_name in item.name), None)
        if device is None:
            names = ", ".join(item.name or item.address for item in devices[:8]) or "none"
            raise RuntimeError(f"No BLE device named {self.device_name} was found. Visible devices: {names}")

        print(f"[BLE] connecting to {device.name or device.address}")
        async with BleakClient(device) as client:
            if not client.is_connected:
                raise RuntimeError("BLE connection failed")

            data = payload.encode("utf-8")
            for offset in range(0, len(data), BLE_CHUNK_SIZE):
                chunk = data[offset : offset + BLE_CHUNK_SIZE]
                # Write WITH response: it blocks until the Wio ACKs each chunk,
                # so every byte lands before this `async with` exits and tears
                # down the link. Write-without-response (response=False) returns
                # instantly on macOS/CoreBluetooth and the buffered packet gets
                # dropped on the immediate disconnect — the device's onWrite
                # never fires. Fall back to no-response only if the stack rejects
                # an acked write on this characteristic.
                try:
                    await client.write_gatt_char(RX_UUID, chunk, response=True)
                except Exception:
                    await client.write_gatt_char(RX_UUID, chunk, response=False)

            # Settle before disconnect so the final frame is processed (covers
            # the no-response fallback path, which isn't acknowledged).
            await asyncio.sleep(0.3)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "AIQuotaDashboard/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def send_json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length > MAX_BODY_BYTES:
            raise ValueError("Request body too large")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/quota":
            try:
                self.send_json(200, read_quota())
            except Exception as error:
                self.send_json(500, {"error": "quota_read_failed", "message": str(error)})
            return

        if parsed.path == "/api/ble/status":
            self.send_json(200, ble_worker.snapshot() if ble_worker else {"enabled": False, "status": "disabled"})
            return

        self.serve_static(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/ble/sync":
            if ble_worker:
                ble_worker.trigger_sync()
                self.send_json(200, {"ok": True})
            else:
                self.send_json(400, {"ok": False, "error": "ble_disabled"})
            return

        if parsed.path != "/api/quota":
            self.send_error(404, "Not found")
            return

        try:
            payload = self.read_json_body()
            write_quota(payload)
            if ble_worker:
                ble_worker.trigger_sync()
            self.send_json(200, {"ok": True})
        except Exception as error:
            self.send_json(400, {"error": "quota_update_failed", "message": str(error)})

    def serve_static(self, request_path):
        if request_path == "/":
            request_path = "/preview.html"

        relative = unquote(request_path).lstrip("/")
        file_path = (ROOT / relative).resolve()

        try:
            file_path.relative_to(ROOT)
        except ValueError:
            self.send_error(404, "Not found")
            return

        if not file_path.is_file():
            self.send_error(404, "Not found")
            return

        content = file_path.read_bytes()
        mime_type = MIME_TYPES.get(file_path.suffix.lower()) or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        cache_control = "no-store" if file_path.name == "preview.html" else "public, max-age=60"

        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def parse_args():
    parser = argparse.ArgumentParser(description="Wio Terminal AI quota dashboard server")
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT)
    parser.add_argument("--ble", action="store_true", default=DEFAULT_BLE_ENABLED, help="auto-sync quota data to Wio Terminal over BLE")
    parser.add_argument("--ble-name", default=BLE_NAME, help="BLE device name to connect")
    parser.add_argument("--ble-interval", type=int, default=10, help="seconds between BLE sends")
    return parser.parse_args()


def main():
    global ble_worker

    args = parse_args()
    if args.ble:
        ble_worker = BleAutoSync(device_name=args.ble_name, interval=args.ble_interval)
        ble_worker.start()
        print(f"BLE auto-sync enabled for device: {args.ble_name}")
    else:
        print("BLE auto-sync disabled. Start with --ble to send data from the server.")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), DashboardHandler)
    print(f"AI quota dashboard server: http://127.0.0.1:{args.port}/preview.html")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped")
    finally:
        if ble_worker:
            ble_worker.stop()
        server.server_close()


if __name__ == "__main__":
    main()
