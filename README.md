# IP Camera Resource URL Finder

A tiny, zero-dependency tool for finding the stream and snapshot URL of an IP
camera. Search a bundled database of 17,000+ known URL patterns by brand or
model, fill in your camera's host and credentials, and copy a ready-to-use URL.

Useful when you are setting up a camera you own in an NVR, VMS, VLC, ffmpeg, or
Home Assistant, and the manufacturer never documented the RTSP/HTTP path.

## Features

- Search 3,688 brands and 73,000+ model aliases by partial keyword.
- A credentials/parameters form (host, port, username, password, channel,
  width, height, token) that fills every result's placeholders live as you
  type, with no page reload.
- Multi-select type filter: RTSP, HTTP, Image (snapshot), RTMP, Other, each
  showing a live count of how many matches fall in that bucket.
- Guided liveness check: a second after you stop typing, the type filters fill
  in with per-type counts and briefly pulse to prompt a choice. Pick a type
  (RTSP, HTTP, ...) and only those URLs are revealed and probed against your
  host - lit up green when reachable at that exact URL, red-bordered when not.
  Scanning just the type you care about lets you narrow dozens of candidate
  patterns down to the one your camera actually answers on.
- One-click copy of the finished URL.
- Runs entirely on `localhost`. Credentials never leave your machine; the
  database is opened read-only.
- No pip installs and no internet access required. Python standard library only.

## Requirements

- Python 3.8 or newer.
- A modern web browser.

## Quick start

```bash
git clone https://github.com/andyg2/ip-camera-resource-url-finder.git
cd ip-camera-resource-url-finder

python server.py            # serves http://127.0.0.1:8000
python server.py 8080       # or choose a port
```

Open http://127.0.0.1:8000, search for your camera's brand or model, fill in the
form, and copy a URL.

Example: search `hikvision`, set host `192.168.1.10`, username `admin`,
password `secret`, channel `1`, then copy:

```
rtsp://admin:secret@192.168.1.10:554/Streaming/Channels/101
```

## How it works

The database stores URL *patterns* as path/query fragments with placeholder
tokens (no host or scheme). The browser assembles the final URL from the pattern
plus your form values.

### Placeholder tokens

| Token in pattern                     | Filled with                                   |
| ------------------------------------ | --------------------------------------------- |
| `[USERNAME]`                         | Username field (URL-encoded)                  |
| `[PASSWORD]` / `[PASWORD]`           | Password field (URL-encoded; second is a typo present in some records) |
| `[CHANNEL]`, `[CHANNEL+1]`, `[CHANNEL-1]` | Channel field, with the optional offset applied |
| `[WIDTH]` / `[HEIGHT]`               | Width / Height fields (default 1920 / 1080)   |
| `[AUTH]`                             | `base64(username:password)` for HTTP Basic    |
| `[TOKEN]`                            | Token field (left unfilled if blank)          |

### Credential placement

- If a pattern already contains `[USERNAME]`, `[PASSWORD]`, or `[AUTH]`, your
  credentials are substituted into those positions (typically HTTP query params)
  and are not added anywhere else.
- If a pattern has no credential placeholder (typical of RTSP basic-auth
  streams), credentials are placed in the authority instead:
  `rtsp://user:pass@host:port/path`.

### Port

The form's Port field is an optional override. When it is empty, the pattern's
own port is used; if the pattern has no port, a protocol default is applied
(RTSP 554, HTTP 80, HTTPS 443, RTMP 1935, and so on).

### Type filter

Each result is classified into one bucket:

- **RTSP** - `rtsp`, `rtsps`, `rtp`
- **HTTP** - `http`/`https` video streams (MJPEG, etc.)
- **Image** - `http`/`https` snapshot/still URLs (`.jpg`, `snapshot`, `picture`, ...)
- **RTMP** - `rtmp`, `rtmps`
- **Other** - `dvrip`, `mms`, `bubble`, and anything else

## API

The UI is served by a small JSON endpoint you can call directly:

```
GET /api/search?q=<term>&limit=<n>
```

```json
{
  "count": 1,
  "results": [
    {
      "id": 7316,
      "brand": "Hikvision",
      "url": "/ISAPI/Streaming/channels/102/picture",
      "protocol": "http",
      "port": 80,
      "models": "DS-2CD2387G2, DS-7108HGHI-F1, ..."
    }
  ]
}
```

`q` must be at least 2 characters. `limit` defaults to 300 (max 1000). Search
matches brand name, brand id, and model aliases.

```
GET /api/probe?url=<full url>
```

```json
{ "alive": true, "status": "401" }
```

Checks whether a fully built camera URL is reachable at that exact path, and
is what the UI calls automatically for each visible result. `alive` is true for
any real protocol response other than a 404 (so `200`, `301`/`302`, `401`,
`403` all count - the path exists, even if it wants credentials); it is false
for a `404`, a refused connection, or a timeout (5-second limit). Probing is
done by protocol:

- **RTSP / RTSPS / RTP** - a `DESCRIBE` request, reading the RTSP status line.
  When credentials are present it answers the auth challenge (Digest first,
  falling back to Basic) and re-sends, so the result reflects the exact path:
  `200` means the path exists and the credentials work, `404` means the path is
  wrong. A `401` that cannot be satisfied (no credentials, or they were
  rejected) is still reported as reachable, since the RTSP server answered.
- **HTTP / HTTPS / Image** - a short ranged `GET`, reading the status code
  (redirects are reported as themselves, not followed; self-signed TLS certs
  are accepted since this is a reachability check, not a security boundary).
- **RTMP / dvrip / mms / other** - a plain TCP connect to the port (these have
  no cheap path-level check, so a successful connect is the signal).

The server only ever connects to hosts you searched and filled in, and still
binds to `127.0.0.1` only, so probes originate from your own machine.

## Project layout

| File          | Purpose                                                        |
| ------------- | -------------------------------------------------------------- |
| `server.py`   | Standard-library HTTP server: serves the UI and `/api/search`. |
| `index.html`  | Single-page UI: search, form, filter, and URL building.        |
| `cameras.db`  | SQLite database of brands, stream patterns, and model aliases. |

### Database contents

The bundled `cameras.db` comes from
[StrixCamDB](https://github.com/eduard256/StrixCamDB) and contains:

- **brands** - 3,688 manufacturer entries.
- **streams** - 17,313 URL patterns, each with protocol, port, and notes.
- **stream_models** - 73,238 model aliases linked to stream patterns.
- **presets** - curated "top 150 / 1000 / 5000" most common pattern lists.
- **oui** - 2,406 MAC address prefixes mapped to brands (for identifying a
  camera by its hardware address).

The server only reads from `streams`, `brands`, and `stream_models`; the
`presets` and `oui` tables are bundled for completeness and future use.

## Responsible use

This tool helps you find the stream URL of a camera you own or are explicitly
authorized to access, for example one on your own network. Do not use it to
access cameras or networks you do not have permission to use. You are
responsible for complying with all applicable laws and the terms of service of
any device or network.

## License

This project is licensed under
[Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)](https://creativecommons.org/licenses/by-nc/4.0/);
see the [LICENSE](LICENSE) file for the full text.

The bundled `cameras.db` is from
[StrixCamDB](https://github.com/eduard256/StrixCamDB) by eduard256, distributed
under the same CC BY-NC 4.0 license. Because the dataset ships with this repo,
the whole project adopts that license so the bundle stays compliant.

In short:

- **Attribution.** Credit StrixCamDB (and this project) when you use or
  redistribute either.
- **NonCommercial.** No commercial use.

Latest database:
https://github.com/eduard256/StrixCamDB/releases/download/latest/cameras.db
