# Demo Day Runbook

## The night before

```bash
cd ~/Desktop/cognizant
docker compose up -d --build
docker compose --profile seed run --rm seed
```

Build once, the night before, on a network you trust. Then confirm and leave it running:

```bash
docker compose ps          # want: 10 rows, all Up
./demo.sh status
```

Do not rebuild on demo morning. There is no reason to, and it's the one step that needs the internet.

## 30 minutes before

```bash
docker compose ps                              # all Up?
./demo.sh status                               # feed state
docker compose --profile seed run --rm seed    # fresh data, ~20 seconds
```

Reseed so the board starts clean and your numbers match what you rehearsed. Then open http://localhost:5173 and log in as `baiju@cognisupply.in` / `inbound2026`.

If something looks wrong:
```bash
docker compose restart          # ~15s, keeps all data
docker compose logs -f --tail=50
```

Full reset, only if you must: `docker compose down && docker compose up -d && docker compose --profile seed run --rm seed`.

## Have these open before you start talking

| Window | Why |
|---|---|
| Chrome → localhost:5173 | the demo |
| Terminal 1, in the repo | `./demo.sh` commands |
| Terminal 2 (optional) | `docker compose logs -f dock-worker` — proof the solver is real |

That second terminal is worth its screen space. When you fire a delay and the audience sees `re-plan [cp-sat OPTIMAL]` scroll past in real time, the claim stops being a claim.

## Driving the demo

Start the world moving:
```bash
./demo.sh feed on
```

Then, one word per moment:

| Say this | Type this | What they see |
|---|---|---|
| "A truck slips its ETA." | `./demo.sh delay` | the yard re-solves around it |
| "A dock goes down." | `./demo.sh block` | routed around, no human |
| "Capacity returns." | `./demo.sh unblock` | picked up on the next re-plan |
| "Monday morning rush." | `./demo.sh surge` | queued by priority |
| "Outbound competes for the same doors." | `./demo.sh rush` | both directions, one solve |
| "A supplier overcharges us." | `./demo.sh price` | exception, not a payment |
| "They over-bill quantity." | `./demo.sh qty` | exception |
| "The PO reference is unreadable." | `./demo.sh missing-po` | routed to a human |

`./demo.sh step` advances the world exactly one tick while paused — useful if you want to narrate a single event slowly.

Every one of those is a real state change through a real endpoint. Nothing on that menu fakes a screen.

## The Docker angle — new, and worth using

You now have something to say when a judge asks "is this a prototype or could it actually ship?"

Open Terminal 2 and run `docker compose ps` in front of them:

> "Ten containers. The whole system — four APIs, three workers, Postgres, Redis, the frontend. It's not a script on my laptop, it's the same images that would go to ECS. Swapping to RDS and ElastiCache is two environment variables."

And the integration point, which is the stronger one:

> "The simulator holds a database connection and is forbidden to use it. Every truck it creates goes through the same authenticated endpoint a real WMS would call. So integrating SAP EWM isn't a rewrite — it's pointing a different client at the same API and turning the simulator off."

## Two honest cautions

1. **Don't run `./run.sh start` and Docker at the same time.** Both publish 8001–8004 and 5173. Pick Docker on demo day — fewer moving parts, no venv, no `npm run dev`.
2. **`./demo.sh` targets 127.0.0.1 hardcoded.** Fine for demo day since everything is local. It only breaks if you deploy to a cloud host — that's the two-line fix I mentioned earlier, and it isn't needed for this.
