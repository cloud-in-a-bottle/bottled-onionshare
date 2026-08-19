# bottled-onionshare

[OnionShare](https://onionshare.org/) packaged as a Cloud in a Bottle app.
Share files, receive files, host a static website, or run a chat room over
Tor hidden services, all managed from a small admin panel gated by your
Cloud in a Bottle login.

## What you get

- Share files: generate a `.onion` address where recipients download files
  you upload.
- Receive files: an anonymous dropbox; incoming uploads are saved to your
  app data.
- Website: serve a folder of uploaded files as a `.onion` website.
- Chat: an ephemeral chat room that keeps nothing.

Each share can be public (URL only) or protected by a per-share private key
the recipient must enter. A share can also be marked persistent, so the same
`.onion` address comes back after a restart.

## Usage

Open the app, click New share, pick a mode, upload files if the mode needs
them, then Start. Copy the `.onion` URL (and the private key, if the share is
not public) and send it to your recipient over a channel they trust, such as
Signal or email. They open it in Tor Browser.

Stop a share when you are done with it, or delete it to remove its staged
files and any saved session key. Recipients reach shares directly over Tor;
that traffic never passes through the Cloud in a Bottle router.

## Caveats

- Starting a share brings up a Tor hidden service, which can take a few
  seconds to become reachable.
- Receive mode accepts arbitrary uploads; treat received files as untrusted.
- A running share's `.onion` address and private key are visible to anyone
  who can open the admin panel, which is behind your Cloud in a Bottle login.

## Data

Persistent app data holds share metadata, files staged for share and website
modes, files uploaded to you in receive mode, and the session keys for
persistent shares. Regenerable per-process scratch is kept in temporary,
non-backed-up storage. Deleting a share removes its staged files and its
persistent session key.

## Resources

1 GB RAM, 2 CPU cores.

## License

OnionShare is licensed under the GNU General Public License v3.0. Because this
image bundles OnionShare, the image as a whole is distributed under the
GPL-3.0 (see LICENSE). The packaging files original to this repository are
additionally offered under the MIT License; see NOTICE.
