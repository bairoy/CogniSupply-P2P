# Design Reference — NOT the app itself

The files in this folder are exported HTML/design output from Claude
Design (or wherever the visual design was produced). They exist to
communicate:

- visual language (colors, typography, spacing, component styling)
- layout/information architecture (which screens, what's on each)
- component structure (what a trailer card, a dock board, an exception
  row, etc. should look like)

**They are not wired to any real data, any real API, or any real
WebSocket connection.** Do not treat this as a starting codebase to
patch — treat it as a visual/structural spec to rebuild properly against
`docs/api-contract.md` and `docs/redis-contract.md`.

When building the real frontend app (in `frontend/`, alongside this
folder, not inside it):
- Match this reference's visual design and layout as closely as
  reasonable.
- Replace every hardcoded/mock value with a real call to an endpoint in
  `docs/api-contract.md`.
- Replace any "live update" simulation with a real WebSocket connection
  to `/ws/dashboard`, following the event envelope shape in
  `docs/redis-contract.md` §2.
- If this reference shows data, fields, or entities that don't exist in
  `backend/schema.sql` or `docs/api-contract.md`, flag it — don't
  silently invent a matching backend capability.
