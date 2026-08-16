# The Simulator: Playing the Outside World

## The problem it solves

Your system is built to react to the outside world. Trucks report their position. Guards scan them in at the gate. Warehouse staff count what came off. Suppliers email invoices. Banks confirm payments.

In a hackathon, none of those exist. There's no truck, no GPS box, no supplier, no bank.

So you wrote one program that pretends to be all of them. That's the **simulator** (or WMS feed). It's an actor playing the role of the outside world.

## How it actually behaves

It wakes up every 3 seconds and asks your database one question: *"what should be happening right now?"*

Then it does those things — by calling your system's normal APIs, exactly the way a real trucking company or supplier would.

There is no script and no recording. Nobody wrote a file saying "at 10:00 the truck arrives, at 10:05 it docks." It works it out fresh each time by looking at what state everything is in.

## One truck's whole life

1. **Tick 1.** Database says `TRL-1001` is `EN_ROUTE`. So the simulator works out where it should be now — a bit closer than last time — and calls `POST /trailers/TRL-1001/tracking` with a latitude and longitude. Your dock worker notices and starts planning which door to give it.
2. **Ticks 2–200.** Same thing, over and over. The truck crawls across the map. That's the moving dot you see.
3. **It gets close.** `POST /arrive` — the truck is at your gate now.
4. **Its dock slot comes due.** `POST /dock` — it pulls into `DOCK-11`.
5. **A few minutes later.** `POST /unload` with a quantity, say 750 units. Your system writes the goods receipt. E2 is done.
6. **Some minutes after that.** The simulator changes hats and becomes the supplier: `POST /invoices`, billing you for that delivery.
7. **Your match worker wakes up.** It compares three documents — the PO, the goods receipt, the invoice. Approve, or raise an exception.
8. **If approved.** The simulator changes hats again and becomes the bank: `POST /payments/{id}/pay`.

That's the full loop. The simulator played the trucking company, the warehouse, the supplier and the bank. Your system did all the thinking in between.

## The part you asked about — coded or stored?

**Coded.** Two examples of what "coded" means here:

The truck's position isn't looked up in a table of route points. Each tick it just moves 12% of the way toward its destination. Simple arithmetic, running live. That's why the route line on your map is smooth — it's being drawn as it happens.

Which supplier sends a wrong invoice isn't random either, and isn't stored. It takes the PO number, hashes it, and reads the answer off that hash. PO-1042 will always produce a price mismatch — today, tomorrow, on any machine. That's deliberate: it means you can rehearse a demo and see the same story, and it's what makes your eval harness's answer key trustworthy.

The only thing genuinely stored is the reference data your business needs to exist at all: 8 suppliers, 15 materials, 14 dock doors, 15 locations, 5 users. Written once by the seed script. That's not the feed — that's your company.

## Why this design wins you marks

The simulator has a database connection. It could cheat and insert rows directly — instant, perfect-looking data.

**It never does. It goes through the front door, like everyone else.**

That means every three seconds, your authentication is being tested. Your validation is being tested. Your transactions, your event publishing, your dock scheduler, your match engine — all being exercised by real traffic.

So when you say "it works," you're not saying "the data looks right in the database." You're saying the system handled real requests correctly.

And the payoff a judge will care about: to go live with a real customer, you point their actual WMS at the same endpoints and delete the simulator. Nothing else changes, because nothing downstream ever knew the difference.
