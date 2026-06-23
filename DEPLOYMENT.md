# polybtc — live250 deployment (AWS Dublin)

Operational runbook for the `config.live250.yaml` test deployment on an AWS
EC2 instance in Dublin. Covers the box setup, how to SSH in, run the bot, view
the dashboard, the measured latency, and the things that will bite you if you
forget them.

> Status at time of writing: box fully provisioned and verified; live trading
> service is **installed but stopped**. Starting it is a manual action (see
> [Running the bot](#running-the-bot)).

---

## 1. The box at a glance

| | |
|---|---|
| Provider / region | AWS EC2, `eu-west-1` (**Dublin, Ireland**) |
| Instance type | **t3.small** (2 vCPU burstable, 2 GB RAM) |
| OS | **Ubuntu 24.04.4 LTS**, Python **3.12.3** |
| Public IPv4 | **`176.34.94.101`** |
| Login user | **`ubuntu`** |
| SSH key | `~/Desktop/poly.pem` (mode `400`, ed25519) |
| Repo path on box | `/home/ubuntu/polybtc` |
| Python venv | `/home/ubuntu/polybtc/.venv` |
| Swap | 2 GB swapfile (OOM backstop) |
| Disk | ~20 GB gp3 (encrypted) |

**Why Dublin and not Tokyo/US:** jurisdiction (see §6) rules out the US
entirely, and Dublin's order-leg latency to Polymarket turned out excellent
(see §5). Why t3.small over t3.micro: `web3` + `pandas` + WebSocket buffers
need RAM headroom; OOM-killing a bot holding live positions is the worst
failure mode.

---

## 2. SSH access

```bash
ssh -i ~/Desktop/poly.pem ubuntu@176.34.94.101
```

**Important — IP allowlist.** The security group permits SSH (port 22) **only
from your current IP**. If your connection/VPN changes your public IP, SSH will
hang/time out. Fix it in the AWS console:

> EC2 → Instances → (your instance) → **Security** tab → security group →
> **Edit inbound rules** → SSH (22) → Source = **My IP** → **Save**.

Check your current public IP with `curl ifconfig.me`. The IP used during setup
was `115.129.146.162`.

**Optional hardening:** move the key out of Downloads/Desktop into `~/.ssh/`,
and add an alias in `~/.ssh/config`:

```
Host polybtc
    HostName 176.34.94.101
    User ubuntu
    IdentityFile ~/.ssh/poly.pem
```

Then just `ssh polybtc`. (For an IP-independent setup, AWS SSM Session Manager
removes the need for any inbound SSH rule — not set up yet, ask if you want it.)

---

## 3. Running the bot

The bot is installed as a **systemd service** (`polybtc.service`), enabled to
start on boot and auto-restart on crash. Live mode trades **real money**.

```bash
# GO LIVE:
sudo systemctl start polybtc

# watch the live log stream:
journalctl -u polybtc -f

# stop:
sudo systemctl stop polybtc

# status / has it been running?:
systemctl status polybtc
```

**Manual / foreground run** (useful for debugging; use `tmux` so it survives
disconnect). Do **not** run this while the systemd service is also running —
they would double-trade the same wallet.

```bash
sudo systemctl stop polybtc          # ensure the service is off first
cd ~/polybtc
tmux new -s bot
.venv/bin/python -m bot.main --live --config config.live250.yaml
#   detach: Ctrl-b then d   |   reattach: tmux attach -t bot
#   paper mode (no real orders): drop the --live flag
```

Each (re)start writes a new `logs/session_<unixtime>.log`. Cash and open
positions persist across restarts via `state.json`; the per-session log does
not (it resets each launch).

---

## 4. Dashboard (view the box's logs from your desktop)

The dashboard runs on the box as `polybtc-dashboard.service`, bound to
`127.0.0.1:8789` (localhost-only — never exposed publicly). View it from your
desktop through an SSH tunnel:

```bash
# open a tunnel (keep this terminal open):
ssh -i ~/Desktop/poly.pem -L 8789:localhost:8789 ubuntu@176.34.94.101
```

Then open **http://localhost:8789** in your browser. Use the **session
selector** dropdown to pick the live session (newest first; auto-selected by
default). The page auto-refreshes, so live equity / fills / per-leg attribution
stream in.

Dashboard service controls (on the box):

```bash
sudo systemctl restart polybtc-dashboard
systemctl status polybtc-dashboard
```

---

## 5. Latency findings (measured in-region from the Dublin box)

The full signal-to-order path, **Tokyo → Dublin → Polymarket**:

| Leg | What | Measured |
|---|---|---|
| **Signal in** (Tokyo → Dublin) | Binance spot mirror + perp, TCP RTT | **~199 ms RTT ≈ 100 ms one-way** |
| Compute + sign | fair value + native EIP-712 signing | ~1–2 ms |
| **Order out** (Dublin → Polymarket) | Cloudflare **Dublin** PoP (1.8 ms to edge); warm round trip to origin | **~17 ms RTT ≈ 8–9 ms one-way** |

**Whole trade ≈ ~110 ms one-way** (BTC ticks in Tokyo → order lands at
Polymarket); ~120 ms to order-acknowledged.

Key takeaways:

- **~90% of the latency is the Tokyo→Dublin signal hop (~100 ms).** Binance's
  matching engine is in AWS Tokyo (`ap-northeast-1`); there is no way around
  the physical distance from Dublin.
- **The order leg is excellent (~17 ms warm).** Polymarket's CLOB is fronted by
  Cloudflare anycast; from Dublin you enter at the Dublin PoP and the path to
  origin is short. You **cannot** colocate next to Polymarket's matching engine
  (it's behind Cloudflare), so this is about as good as the order leg gets.
- **Don't over-invest in latency for this test.** The strategy's own research
  says the edge survives being 1–5 *seconds* late (+14¢ vs +16¢/share). 110 ms
  is comfortably inside that.
- Moving the box to Tokyo would cut the signal leg to ~0 but push the order leg
  out to ~100+ ms (Tokyo→Polymarket), so it's roughly a wash — Dublin is a
  sound choice. Revisit only if live capture-rate proves latency-bound (compare
  the live FAK fill-rate against the paper fill-rate).

---

## 6. Jurisdiction / geo-blocks (this is a hard gate, not just latency)

Two independent geo-filters stack on this strategy:

- **Polymarket** blocks: **Australia, US, France, Netherlands** (among others).
  Verified: from a Melbourne home connection, `clob.polymarket.com` resolves to
  a dead AU IP and times out — **you cannot run this from home in Australia.**
- **Binance perp feed** (`fstream.binance.com`, the perp-lead signal) is blocked
  from **US** IPs.

Region implications:

| Region | Polymarket | Binance perp | Usable? |
|---|---|---|---|
| `us-east-1` (US) | blocked | blocked | **No** |
| `eu-west-3` (Paris) | blocked | ok | **No** |
| Amsterdam (NL) | blocked | ok | **No** |
| **`eu-west-1` (Dublin)** | **ok** | **ok** | **Yes — chosen** |
| `eu-central-1` (Frankfurt) | ok | ok | Yes (alt) |
| `ap-northeast-1` (Tokyo) | unverified | unverified | maybe |

The Dublin box was verified live: the paper smoke test connected to the perp
feed *and* tracked 6 Polymarket BTC markets, confirming both venues are
reachable from `eu-west-1`.

---

## 7. Wallet / pre-flight

Read-only pre-flight against the live CLOB confirmed:

| | |
|---|---|
| Signature type | **3** (POLY_1271 deposit wallet) — type 0/EOA is rejected by the V2 venue |
| Wallet address | `0x4e7996f258270c741ae409435d7ba20cc07b22e3` |
| Collateral | **$209.33 pUSD** (funded) |

Notes:

- `seed_from_wallet: true` + `reference_equity: null` → the kill switch and
  position sizing pivot around the **real funded balance** (~$209), not the
  $250 label. It will simply trade a hair smaller. Top up the wallet to $250 if
  you want the full tier.
- **`signature_type 3` means on-chain auto merge/redeem is DISABLED.** Winning /
  complete positions must be merged/redeemed via the **Polymarket UI**.
  Over a multi-day run, redeem resolved positions periodically or collateral
  gets tied up in resolved tokens.
- The native `CoinCurveECCBackend` signing backend is active (~0.15 ms/order,
  not the 3.5 ms pure-Python fallback). The bot logs this at startup — watch for
  it.

---

## 8. Risk controls & monitoring

- **Kill switch:** halts and cancels everything if session equity drops more
  than 30% (capped to a $40–$75 loss at the ~$250 tier per `config.live250.yaml`).
- **Feed-staleness guard:** quotes are pulled if the Binance feed stalls beyond
  `risk.max_feed_age_sec`; sniper/scalper refuse books older than 15 s.
- **FEE CHECK:** every live taker fill logs the real fee vs the modeled fee, so
  a fee-schedule change is caught.
- **Session summary:** printed on exit / kill-switch — per-leg realized P&L,
  maker vs taker volume, fees, FAK fill rate, adverse-selection fills.

What to watch in the first live session:

1. Startup line shows native signing backend (not pure-Python).
2. `live: seeded equity baseline from wallet collateral: $209.xx`.
3. First `FILL` lines and their `FEE CHECK` matching the model.
4. **Live FAK fill rate vs paper** — the real test of whether latency/competition
   is eating capture.

---

## 9. Security checklist

- `.env` (holds the wallet private key) is on the box at mode **`600`**,
  owner-only. **Never commit it** (it's gitignored) and never bake it into an AMI.
- Inbound security group: **SSH (22) from your IP only**. Nothing else inbound —
  the dashboard is reached via SSH tunnel, not an open port.
- EBS volume is **encrypted** (the private key lives on this disk).
- SSH key `poly.pem` is mode `400`; keep it off shared/synced locations.

---

## 10. Things to know / next steps

- **t3 is burstable, shared-tenancy.** Fine for this test. If you later chase the
  FIFO race seriously, move to a non-burstable, network-consistent instance
  (`c7i` / `c6in` / `m7i`) — not a bigger t3.
- **`presign` is off** (`config.live250.yaml`). The presigner still pre-warms the
  CLOB market-info cache (removes a cold-start spike). Turning `enabled: true`
  keeps signing fully off the hot path but snaps stake down to the nearest
  bucket — leave off until you trust live sizing.
- **IP churn breaks SSH.** Pick a stable network or set up SSM (ask).
- **Re-run the research pipeline weekly** — the snipe edge decays with the
  competitive landscape (see `README.md` / `research/REPORT.md`).

---

## Quick reference

```bash
# connect
ssh -i ~/Desktop/poly.pem ubuntu@176.34.94.101

# go live / watch / stop
sudo systemctl start polybtc
journalctl -u polybtc -f
sudo systemctl stop polybtc

# dashboard tunnel  ->  http://localhost:8789
ssh -i ~/Desktop/poly.pem -L 8789:localhost:8789 ubuntu@176.34.94.101
```
