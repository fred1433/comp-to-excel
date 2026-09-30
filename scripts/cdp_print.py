"""Print a web page to PDF through a local Chrome DevTools endpoint, in a background tab.

Used once, by us, to save listing pages the way an appraiser saves an MLS sheet to a job folder.
The PDFs stay local (sources/private/, git-ignored); only URL, date and SHA-256 are published.
"""
import base64, json, sys, time, urllib.request
import websocket

CDP = "http://127.0.0.1:9222"

def browser_ws():
    return json.load(urllib.request.urlopen(CDP + "/json/version"))["webSocketDebuggerUrl"]

def main(url, out):
    ws = websocket.create_connection(browser_ws(), timeout=60, suppress_origin=True)
    n = 0
    def call(method, params=None, session=None):
        nonlocal n
        n += 1
        msg = {"id": n, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        ws.send(json.dumps(msg))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == n:
                if "error" in r:
                    raise RuntimeError(r["error"])
                return r["result"]
    tid = call("Target.createTarget", {"url": "about:blank", "background": True})["targetId"]
    try:
        sid = call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        call("Page.enable", session=sid)
        call("Page.navigate", {"url": url}, session=sid)
        time.sleep(12)
        for _ in range(6):
            call("Runtime.evaluate", {"expression": "window.scrollBy(0, 1200)"}, session=sid)
            time.sleep(1)
        pdf = call("Page.printToPDF", {"printBackground": False, "preferCSSPageSize": False}, session=sid)["data"]
        open(out, "wb").write(base64.b64decode(pdf))
    finally:
        urllib.request.urlopen(urllib.request.Request(f"{CDP}/json/close/{tid}"))
        ws.close()

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
