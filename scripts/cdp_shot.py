"""Screenshot a page in a background tab of a local Chrome (DevTools on 127.0.0.1:9222).

usage: cdp_shot.py URL OUT.png WIDTH [HEIGHT|full] [--click SELECTOR]
"""
import base64, json, sys, time, urllib.request
import websocket

CDP = "http://127.0.0.1:9222"


def main(url, out, width, height="900", click=None):
    ws = websocket.create_connection(json.load(urllib.request.urlopen(CDP + "/json/version"))["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
    n = 0
    def call(method, params=None, session=None):
        nonlocal n
        n += 1
        msg = {"id": n, "method": method, "params": params or {}}
        if session: msg["sessionId"] = session
        ws.send(json.dumps(msg))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == n:
                if "error" in r: raise RuntimeError(r["error"])
                return r["result"]
    tid = call("Target.createTarget", {"url": "about:blank", "background": True})["targetId"]
    try:
        sid = call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        w = int(width); mobile = w < 600
        h = 900 if height == "full" else int(height)
        call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2 if mobile else 1, "mobile": mobile}, sid)
        call("Page.enable", session=sid)
        call("Page.navigate", {"url": url}, sid)
        time.sleep(3)
        if click:
            call("Runtime.evaluate", {"expression": f"(b => {{ b.click(); window.scrollTo(0, b.getBoundingClientRect().top + scrollY - 20); }})(document.querySelector({json.dumps(click)}))"}, sid)
            time.sleep(0.8)
        params = {"format": "png"}
        if height == "full":
            m = call("Page.getLayoutMetrics", session=sid)["cssContentSize"]
            params.update({"captureBeyondViewport": True, "clip": {"x": 0, "y": 0, "width": w, "height": m["height"], "scale": 1}})
        data = call("Page.captureScreenshot", params, sid)["data"]
        open(out, "wb").write(base64.b64decode(data))
    finally:
        urllib.request.urlopen(f"{CDP}/json/close/{tid}", timeout=10)
        ws.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    click = None
    if "--click" in args:
        i = args.index("--click"); click = args[i + 1]; args = args[:i] + args[i + 2:]
    main(*args, click=click)
