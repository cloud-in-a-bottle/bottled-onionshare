# openhost-onionshare

[OnionShare](https://onionshare.org/) packaged as an OpenHost app.

Share files, receive files, host a static website, or run a chat room
over Tor hidden services — all managed from a tiny admin UI gated by
your OpenHost login. Recipients connect via a `.onion` URL using Tor
Browser; that traffic never touches the OpenHost router.

## What you get

- **Share files** — generate a `.onion` that lets recipients download
  files you've uploaded.
- **Receive files** — anonymous dropbox. Incoming uploads land under
  `/data/app_data/onionshare/received/<share_id>/`.
- **Website** — serve a static folder as a `.onion` site.
- **Chat** — ephemeral chat room, nothing persisted.

Each share can be **public** (URL-only access) or default (URL + a
per-share private key the recipient must enter). Each share can also
be marked **persistent**, which writes a session file so the same
`.onion` address comes back the next time the share is started.

## Architecture

The container runs two things:

1. `server.py` — a small stdlib HTTP server on port 8080 that serves
   the admin UI. Bound to 127.0.0.1 on the host by OpenHost; all
   requests go through the OpenHost router which enforces the owner's
   login before proxying here.
2. One `onionshare-cli` subprocess per running share. Each of those
   spawns its own `tor` process, creates an ephemeral or persistent
   hidden service, and serves the share's content directly over Tor.
   We scrape stdout to pull the `.onion` URL and (for non-public
   shares) the private key to display in the admin UI.

No extra host ports are bound. No Linux capabilities or device
pass-through required. All outbound Tor traffic goes over the standard
container bridge.

## Data layout

```
/data/app_data/onionshare/
  state.json                    # share metadata
  shares/<id>/                  # uploaded files for share/website modes
  received/<id>/                # files recipients upload to you
  persistent_sessions/<id>.json # onionshare-cli session files
/data/app_temp_data/onionshare/
  run/                          # per-process scratch (HOME for onionshare-cli)
```

## Usage

Open the app on your OpenHost instance, click **New share**, pick a
mode, upload files if applicable, then **Start**. Copy the `.onion`
URL (and private key, if not public) to your recipient over a
side-channel they trust — Signal, email, etc. They open it in Tor
Browser.

Stop the share when you're done. Delete the share to remove staged
files and any persistent session key.

## Security notes

- The `.onion` address and private key for a live share are visible to
  anyone with access to the admin UI. The admin UI is behind your
  OpenHost login.
- Persistent shares store their Tor keys in `persistent_sessions/`.
  Treat that directory like you'd treat any secret.
- Receive mode accepts arbitrary uploads. Don't open received files
  from untrusted sources without the usual precautions.
- The admin server talks plain HTTP to localhost; OpenHost's reverse
  proxy provides TLS to the outside world.

## Licensing

OnionShare is licensed GPL-3.0. This wrapper repo is distributed under
the same terms.
