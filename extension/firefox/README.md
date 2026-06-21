# itamx EL AL Award Inspector

Firefox WebExtension for logged-in EL AL award pages.

## Load

### Temporary Install

1. Open `about:debugging#/runtime/this-firefox`.
2. Click `Load Temporary Add-on`.
3. Select `extension/firefox/manifest.json`.
4. Open `booking.elal.com` and run an EL AL bonus search.

### Permanent Unsigned Install

This works only in Firefox Developer Edition, Nightly, or ESR. Normal Firefox
Release requires Mozilla-signed extensions.

1. Open `about:config`.
2. Set `xpinstall.signatures.required` to `false`.
3. Build the local XPI:

   ```bash
   extension/firefox/scripts/build-xpi.sh
   ```

4. Open the generated `.xpi` from `extension/firefox/dist/`.
5. Accept the installation prompt.

## What It Captures

The extension injects a page hook and listens for:

- `POST /bfm/service/extly/booking/search/points/fast`
- `GET /bfm/service/extly/booking/search/points/outbound`
- `GET /bfm/service/extly/booking/search/points/inbound`

Rows are normalized from:

- `data.trip.outbound.*Bounds.bounds[].fares[]`
- `data.trip.returnBound.*Bounds.bounds[].fares[]`

`seats_left` is EL AL's `nbSeatLeft` field on each fare.

## Points Award Buckets

- Coach: `E`
- Premium: `A`
- Business: `X`

The overlay filters by points-award bucket, minimum seats, max points, and free text
matching flight, RBD, airport, aircraft, cabin, or fare family. Clicking a date
shows the normalized row JSON, including segments.

When `Embed` is enabled, the extension also annotates EL AL's own result cards:

- a detail band per flight card with every returned fare/RBD
- chips next to matching point prices
- other award buckets such as premium `B/P/Q/W` and business `C/I/Z`
