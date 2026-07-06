# PWA manual QA checklist

Run over **HTTPS** with `DEBUG=false` and a valid TLS certificate. Service workers require a secure context.

## iPad (Safari)

- [ ] Open site in Safari
- [ ] Share → **Add to Home Screen**
- [ ] Launch from home screen (standalone, no browser chrome)
- [ ] Browse media list and open a player
- [ ] Tap **Save for offline** — button shows "Saving…" then "Saved"
- [ ] Enable airplane mode — offline banner appears
- [ ] Play saved media from `/offline` list
- [ ] Seek within video/audio while offline
- [ ] Tap **Remove** on offline list — item disappears, storage info updates
- [ ] Re-enable network — app loads fresh content

## Chromebook (Chrome)

- [ ] Open site in Chrome
- [ ] Install app (address bar install icon or ⋮ → **Install QAS Media**)
- [ ] Launch installed app
- [ ] Save media for offline from list and player pages
- [ ] Confirm `/offline` page lists saved items with sizes
- [ ] Go offline (disable Wi‑Fi) — playback works from cache
- [ ] Remove offline item — cache and list stay in sync
- [ ] Deploy an update — new service worker activates after refresh

## Storage quota

- [ ] With device storage nearly full, save shows a clear error (not a silent failure)
- [ ] `/offline` shows device usage when `navigator.storage.estimate` is available

## Notes

Record device OS version, browser version, and any failures below:

| Date | Device | Result | Notes |
|------|--------|--------|-------|
|      |        |        |       |
