"""
Import / export for the REST API tab (no GUI code).

Two file kinds:
  * Portal System REST file  - our own JSON: device connections, "My commands", custom profiles.
  * Postman Collection v2.1   - the common format used by Postman, Insomnia, Bruno, Hoppscotch ...
"""
import datetime
import json
import re
from urllib.parse import urlsplit

FORMAT = "PortalSystem-REST"
FORMAT_VERSION = 1
POSTMAN_SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"

DEVICE_KEYS = ("host", "port", "https", "insecure", "auth", "user", "header", "secret")
PRESET_KEYS = ("group", "name", "method", "path", "body_type", "body", "confirm", "help", "headers")
BUILTIN_NAMES = {"Generic REST device", "Crestron VC-4 (Virtual Control)", "Crestron DM NVX"}
_PH = re.compile(r"\$\{([^:|}{]+?)(?::([a-z]+))?(?:\|([^}]*))?\}")      # same syntax as restcore
_PM_VAR = re.compile(r"\{\{\s*([^{}$][^{}]*?)\s*\}\}")                  # {{var}} but not {{$guid}}


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def clean_preset(p):
    out = {k: p[k] for k in PRESET_KEYS if p.get(k) not in (None, "")}
    out.setdefault("group", "My commands")
    out.setdefault("name", "Unnamed")
    out["method"] = str(out.get("method", "GET")).upper()
    out.setdefault("path", "")
    out.setdefault("body_type", "none")
    return out


def clean_profile(p):
    return {k: v for k, v in p.items() if k != "file" and not k.startswith("_")}


def profile_for_disk(p):
    """What goes into a profile .json file (no export-only marks)."""
    return {k: v for k, v in clean_profile(p).items() if k != "builtin"}


def same_commands(a, b):
    """True if two profiles have the same address settings and commands."""
    keys = ("scheme", "base_path", "auth")
    if any((a.get(k) or "") != (b.get(k) or "") for k in keys):
        return False
    return [clean_preset(x) for x in a.get("presets", [])] == [clean_preset(x) for x in b.get("presets", [])]


# ====================================================================== our own format

def build_export(app_version, profiles, devices, my_commands, include_secrets=False):
    """profiles: profile dicts to include in full (built-in ones carry "builtin": True);
    devices: {profile name: settings dict}; my_commands: {name: [presets]}"""
    devs = []
    for name, d in devices.items():
        if not (d.get("host") or "").strip():
            continue
        dev = {"profile": name}
        for k in DEVICE_KEYS:
            if k == "secret" and not include_secrets:
                continue
            if d.get(k) not in (None, ""):
                dev[k] = d[k]
        devs.append(dev)
    return {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "app": app_version,
        "exported": _now(),
        "contains_secrets": bool(include_secrets and any("secret" in d for d in devs)),
        "profiles": [clean_profile(p) for p in profiles],
        "devices": devs,
        "my_commands": {k: [clean_preset(p) for p in v] for k, v in my_commands.items() if v},
    }


def detect(data):
    if isinstance(data, dict) and data.get("format") == FORMAT:
        return "portal"
    if isinstance(data, dict) and isinstance(data.get("collection"), dict):        # Postman API wrapper
        data = data["collection"]
    if isinstance(data, dict) and isinstance(data.get("info"), dict) and "item" in data:
        schema = str(data["info"].get("schema", ""))
        if "v1" in schema.split("/collection/")[-1][:3]:
            return "postman-v1"
        return "postman"
    if isinstance(data, dict) and "name" in data and "presets" in data:
        return "profile"                                                         # a single profile file
    return None


def parse_import(data):
    """-> dict(kind, profiles, devices, my_commands, warnings) in our own model."""
    kind = detect(data)
    if kind == "portal":
        if int(data.get("version", 1)) > FORMAT_VERSION:
            warn = ["This file was made by a newer version - some parts may be skipped."]
        else:
            warn = []
        profiles = [p for p in data.get("profiles", []) if isinstance(p, dict) and p.get("name")]
        for p in profiles:
            p["presets"] = [clean_preset(x) for x in p.get("presets", []) if isinstance(x, dict)]
        devices = {}
        for d in data.get("devices", []):
            if isinstance(d, dict) and d.get("profile"):
                devices[d["profile"]] = {k: d[k] for k in DEVICE_KEYS if k in d}
        mine = {k: [clean_preset(p) for p in v if isinstance(p, dict)]
                for k, v in (data.get("my_commands") or {}).items() if isinstance(v, list)}
        return {"kind": kind, "profiles": profiles, "devices": devices, "my_commands": mine, "warnings": warn}
    if kind == "profile":
        p = dict(data)
        p["presets"] = [clean_preset(x) for x in p.get("presets", []) if isinstance(x, dict)]
        return {"kind": kind, "profiles": [p], "devices": {}, "my_commands": {}, "warnings": []}
    if kind == "postman":
        coll = data["collection"] if "collection" in data and isinstance(data["collection"], dict) else data
        profile, device, warn = from_postman(coll)
        return {"kind": kind, "profiles": [profile], "devices": {profile["name"]: device} if device else {},
                "my_commands": {}, "warnings": warn}
    if kind == "postman-v1":
        raise ValueError("This is an old Postman v1 collection. Open it in Postman and export it again "
                         "as 'Collection v2.1', then import that file.")
    raise ValueError("This file is not a Portal System REST file, a device profile or a Postman collection.")


def merge_commands(existing, incoming):
    """Same group + name replaces, new ones are added. -> (merged, added, replaced)"""
    out = [dict(p) for p in existing]
    index = {(p["group"], p["name"]): i for i, p in enumerate(out)}
    added = replaced = 0
    for p in incoming:
        k = (p["group"], p["name"])
        if k in index:
            out[index[k]] = p
            replaced += 1
        else:
            index[k] = len(out)
            out.append(p)
            added += 1
    return out, added, replaced


def safe_filename(name):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return (s or "profile")[:80]


# ====================================================================== Postman export

def _to_pm_vars(text, names):
    def rep(m):
        n = m.group(1).strip().replace(" ", "_")
        n = re.sub(r"[^\w.-]", "", n) or "value"
        if n not in names:
            names[n] = (m.group(3) or "").split(",")[0].strip() if m.group(3) else ""
        return "{{" + n + "}}"
    return _PH.sub(rep, text or "")


def _pm_kv(lines, sep):
    out = []
    for line in (lines or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or sep not in line:
            continue
        k, v = line.split(sep, 1)
        out.append((k.strip(), v.strip()))
    return out


def to_postman(profile, presets, device=None, include_secrets=False):
    """One collection for one profile. Folders = command groups. {{baseUrl}} holds the device address."""
    device = device or {}
    names = {}
    scheme = "https" if device.get("https", profile.get("scheme") == "https") else "http"
    host = (device.get("host") or "").strip() or "DEVICE-IP"
    port = int(device.get("port") or profile.get("port") or 0)
    base = f"{scheme}://{host}{':' + str(port) if port else ''}{profile.get('base_path', '')}"
    auth_type = device.get("auth") or profile.get("auth", "none")
    secret = device.get("secret", "") if include_secrets else ""
    user = device.get("user", "")
    variables = [{"key": "baseUrl", "value": base}]
    auth = {"type": "noauth"}
    if auth_type in ("basic", "digest"):
        auth = {"type": auth_type, auth_type: [{"key": "username", "value": "{{username}}", "type": "string"},
                                               {"key": "password", "value": "{{password}}", "type": "string"}]}
        variables += [{"key": "username", "value": user}, {"key": "password", "value": secret}]
    elif auth_type == "bearer":
        auth = {"type": "bearer", "bearer": [{"key": "token", "value": "{{token}}", "type": "string"}]}
        variables.append({"key": "token", "value": secret})
    elif auth_type == "header":
        hname = device.get("header") or profile.get("header_name") or "Authorization"
        auth = {"type": "apikey", "apikey": [{"key": "key", "value": hname, "type": "string"},
                                             {"key": "value", "value": "{{token}}", "type": "string"},
                                             {"key": "in", "value": "header", "type": "string"}]}
        variables.append({"key": "token", "value": secret})
    elif auth_type == "cresnext":
        variables += [{"key": "username", "value": user}, {"key": "password", "value": secret}]

    folders = {}
    order = []

    def folder(name):
        if name not in folders:
            folders[name] = {"name": name, "item": []}
            order.append(name)
        return folders[name]["item"]

    if auth_type == "cresnext":
        login = {"name": "Log in (run this first)", "request": {
            "method": "POST",
            "header": [{"key": "Origin", "value": "{{origin}}"}, {"key": "Referer", "value": "{{origin}}/userlogin.html"}],
            "body": {"mode": "urlencoded", "urlencoded": [{"key": "login", "value": "{{username}}", "type": "text"},
                                                          {"key": "passwd", "value": "{{password}}", "type": "text"}]},
            "url": _pm_url("{{origin}}/userlogin.html"),
            "description": "Crestron devices log in with a form. Postman keeps the cookies it gets back, so run "
                           "this once before the other requests (again if they start returning 302/403)."}}
        folder("Session").append(login)
        u = urlsplit(base)
        variables.append({"key": "origin", "value": f"{u.scheme}://{u.netloc}"})

    for p in presets:
        path = _to_pm_vars(p.get("path", ""), names)
        url = path if re.match(r"https?://", path, re.I) else "{{baseUrl}}" + ("" if path.startswith("/") or not path else "/") + path
        headers = [{"key": k, "value": _to_pm_vars(v, names)} for k, v in _pm_kv(p.get("headers", ""), ":")]
        if auth_type == "cresnext" and not p.get("path", "").startswith("http"):
            headers += [{"key": "Origin", "value": "{{origin}}"}, {"key": "Referer", "value": "{{origin}}/"}]
        req = {"method": p.get("method", "GET"), "header": headers, "url": _pm_url(url)}
        bt, body = p.get("body_type", "none"), p.get("body", "")
        if bt == "json":
            req["body"] = {"mode": "raw", "raw": _to_pm_vars(body, names), "options": {"raw": {"language": "json"}}}
            req["header"].append({"key": "Content-Type", "value": "application/json"})
        elif bt == "raw":
            req["body"] = {"mode": "raw", "raw": _to_pm_vars(body, names)}
        elif bt == "form":
            req["body"] = {"mode": "urlencoded", "urlencoded": [
                {"key": k, "value": _to_pm_vars(v, names), "type": "text"} for k, v in _pm_kv(body, "=")]}
        elif bt == "multipart":
            fd = []
            for k, v in _pm_kv(body, "="):
                if v.startswith("@"):
                    src = v[1:].strip()
                    fd.append({"key": k, "type": "file", "src": "" if _PH.search(src) else src,
                               "description": "choose the file in Postman" if _PH.search(src) else ""})
                else:
                    fd.append({"key": k, "value": _to_pm_vars(v, names), "type": "text"})
            req["body"] = {"mode": "formdata", "formdata": fd}
        desc = "\n\n".join(x for x in (p.get("help", ""),
                                         ("Asks first: " + _to_pm_vars(p["confirm"], names)) if p.get("confirm") else "") if x)
        if desc:
            req["description"] = desc
        folder(p.get("group") or "Commands").append({"name": p.get("name", "Request"), "request": req})

    for n, default in names.items():
        if n not in {v["key"] for v in variables}:
            variables.append({"key": n, "value": default})
    coll = {
        "info": {"name": profile["name"], "schema": POSTMAN_SCHEMA,
                 "description": (profile.get("notes", "") + "\n\nExported from NetworkPocketknife (Portal System). "
                                 "Edit the collection variables (baseUrl, username/password or token, and the "
                                 "values used in the requests) before sending.").strip()},
        "item": [folders[n] for n in order],
        "auth": auth,
        "variable": variables,
    }
    return coll


def _pm_url(raw):
    u = {"raw": raw}
    m = re.match(r"(\{\{[^}]+\}\}|https?://[^/]+)(/.*)?$", raw)
    if m:
        u["host"] = [m.group(1)]
        if m.group(2):
            u["path"] = [s for s in m.group(2).split("?")[0].split("/") if s]
    return u


# ====================================================================== Postman import

def _pm_text(v):
    if isinstance(v, dict):
        return str(v.get("content", ""))
    return str(v or "")


def _pm_vars_in(text):
    """{{name}} -> ${name};  {{$guid}} and friends are left as they are."""
    return _PM_VAR.sub(lambda m: "${" + m.group(1).strip() + "}", text or "")


def _raw_url(u):
    if isinstance(u, str):
        return u
    if not isinstance(u, dict):
        return ""
    if u.get("raw"):
        return u["raw"]
    host = u.get("host", "")
    host = ".".join(host) if isinstance(host, list) else str(host)
    path = u.get("path", "")
    path = "/".join(path) if isinstance(path, list) else str(path)
    proto = u.get("protocol")
    port = f":{u['port']}" if u.get("port") else ""
    q = "&".join(f"{x.get('key')}={x.get('value', '')}" for x in u.get("query", []) if not x.get("disabled"))
    return (f"{proto}://" if proto else "") + host + port + ("/" + path if path else "") + ("?" + q if q else "")


def _pm_auth(a, variables):
    """-> (auth type, user, secret, header name)"""
    if not isinstance(a, dict):
        return None
    t = a.get("type", "noauth")

    def val(key):
        for x in a.get(t, []) if isinstance(a.get(t), list) else []:
            if x.get("key") == key:
                v = str(x.get("value", ""))
                m = re.fullmatch(r"\{\{\s*([^{}]+?)\s*\}\}", v)
                return variables.get(m.group(1), "") if m else v
        return ""
    if t in ("basic", "digest"):
        return t, val("username"), val("password"), ""
    if t == "bearer":
        return "bearer", "", val("token"), ""
    if t == "apikey" and (val("in") or "header") == "header":
        return "header", "", val("value"), val("key") or "Authorization"
    if t in ("noauth", "inherit"):
        return "none", "", "", ""
    return "unsupported:" + t, "", "", ""


def from_postman(coll):
    warn = []
    info = coll.get("info", {})
    name = (info.get("name") or "Imported collection").strip()
    variables = {v.get("key"): str(v.get("value", "")) for v in coll.get("variable", []) if isinstance(v, dict) and v.get("key")}

    requests = []

    def walk(items, groups):
        for it in items or []:
            if not isinstance(it, dict):
                continue
            if isinstance(it.get("item"), list):
                walk(it["item"], groups + [it.get("name", "Folder")])
            elif isinstance(it.get("request"), (dict, str)):
                requests.append((" / ".join(groups) or "Commands", it.get("name", "Request"), it["request"]))
    walk(coll.get("item"), [])
    if not requests:
        warn.append("The collection has no requests.")

    # ---- work out the device address ("base URL")
    raws = [_raw_url(r.get("url") if isinstance(r, dict) else r) for _g, _n, r in requests]
    counts = {}
    for raw in raws:                    # the variable most requests start with, e.g. {{baseUrl}}
        m = re.match(r"\{\{\s*([^{}]+?)\s*\}\}", raw)
        if m and re.match(r"https?://", variables.get(m.group(1), ""), re.I):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    base_var = max(counts, key=counts.get) if counts else None
    if base_var:
        base_url = variables[base_var].rstrip("/")
    else:
        origins = {urlsplit(r).scheme + "://" + urlsplit(r).netloc for r in raws if re.match(r"https?://", r, re.I)}
        base_url = origins.pop() if len(origins) == 1 else ""
        if len(origins) > 0:
            warn.append("The requests use more than one address - they keep their full URLs.")
            base_url = ""
    bu = urlsplit(base_url) if base_url else None

    # ---- collection-level login
    auth = _pm_auth(coll.get("auth"), variables) or ("none", "", "", "")
    if auth[0].startswith("unsupported:"):
        warn.append(f"Login type '{auth[0][12:]}' is not supported - set the login by hand.")
        auth = ("none", "", "", "")
    login_like = [n for g, n, r in requests if isinstance(r, dict) and "userlogin" in _raw_url(r.get("url"))]
    if login_like and auth[0] == "none":
        auth = ("cresnext", variables.get("username", ""), variables.get("password", ""), "")

    presets = []
    for group, rname, r in requests:
        if isinstance(r, str):
            r = {"method": "GET", "url": r}
        raw = _raw_url(r.get("url"))
        if base_var and re.match(r"\{\{\s*" + re.escape(base_var) + r"\s*\}\}", raw):
            path = re.sub(r"^\{\{\s*" + re.escape(base_var) + r"\s*\}\}", "", raw)
        elif base_url and raw.lower().startswith(base_url.lower()):
            path = raw[len(base_url):]
        else:
            path = raw
        if auth[0] == "cresnext" and "userlogin" in raw:
            continue                                  # our client logs in by itself
        p = {"group": group, "name": rname, "method": str(r.get("method", "GET")).upper(),
             "path": _pm_vars_in(path) or "/"}
        hdr = []
        for h in r.get("header", []) or []:
            if isinstance(h, dict) and not h.get("disabled") and h.get("key"):
                k = h["key"]
                if k.lower() == "content-type" and "json" in str(h.get("value", "")).lower():
                    continue
                if auth[0] == "cresnext" and k.lower() in ("origin", "referer", "cookie", "x-crest-xsrf-token"):
                    continue
                hdr.append(f"{k}: {_pm_vars_in(str(h.get('value', '')))}")
        if hdr:
            p["headers"] = "\n".join(hdr)
        body = r.get("body") or {}
        mode = body.get("mode")
        if mode == "raw":
            text = body.get("raw", "")
            lang = ((body.get("options") or {}).get("raw") or {}).get("language", "")
            is_json = lang == "json" or text.strip()[:1] in "{["
            p["body_type"], p["body"] = ("json" if is_json else "raw"), _pm_vars_in(text)
        elif mode == "urlencoded":
            p["body_type"] = "form"
            p["body"] = "\n".join(f"{x.get('key')}={_pm_vars_in(str(x.get('value', '')))}"
                                  for x in body.get("urlencoded", []) if not x.get("disabled"))
        elif mode == "formdata":
            lines = []
            for x in body.get("formdata", []):
                if x.get("disabled"):
                    continue
                if x.get("type") == "file":
                    src = x.get("src")
                    src = src[0] if isinstance(src, list) and src else src
                    lines.append(f"{x.get('key')}=@" + (src or "${" + str(x.get("key")) + ":file}"))
                else:
                    lines.append(f"{x.get('key')}={_pm_vars_in(str(x.get('value', '')))}")
            p["body_type"], p["body"] = "multipart", "\n".join(lines)
        elif mode in ("graphql", "file"):
            warn.append(f"'{rname}': body type '{mode}' is not supported - body left empty.")
        if r.get("auth") and _pm_auth(r["auth"], variables) and _pm_auth(r["auth"], variables)[0] not in ("none", auth[0]):
            warn.append(f"'{rname}' has its own login type - the collection login is used instead.")
        desc = _pm_text(r.get("description")).strip()
        m = re.search(r"(?m)^Asks first: (.+)$", desc)
        if m:                                           # our own export keeps "ask before sending" here
            p["confirm"] = _pm_vars_in(m.group(1).strip())
            desc = (desc[:m.start()] + desc[m.end():]).strip()
        elif p["method"] == "DELETE":
            p["confirm"] = f"Send DELETE {path}?"
        if desc:
            p["help"] = desc[:500]
        presets.append(clean_preset(p))

    profile = {"name": name, "scheme": bu.scheme if bu else "http", "port": 0,
               "base_path": bu.path.rstrip("/") if bu else "", "auth": auth[0],
               "header_name": auth[3] or "Authorization",
               "secret_label": {"bearer": "Token", "header": "Token"}.get(auth[0], "Password"),
               "notes": (_pm_text(info.get("description")).strip()[:400] or f"Imported from Postman: {name}"),
               "presets": presets}
    device = None
    if bu and bu.hostname and "{{" not in bu.netloc:
        device = {"host": bu.hostname, "port": bu.port or 0, "https": bu.scheme == "https", "insecure": True,
                  "auth": auth[0], "user": auth[1], "header": auth[3] or "Authorization"}
        if auth[2]:
            device["secret"] = auth[2]
    left = sorted({m.group(1) for p in presets for m in _PH.finditer(p["path"] + p.get("body", ""))})
    if left:
        warn.append("Values asked for when sending: " + ", ".join(left[:12]) + (" …" if len(left) > 12 else ""))
    return profile, device, warn


def load_file(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def save_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
