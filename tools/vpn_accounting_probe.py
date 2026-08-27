#!/usr/bin/env python3
"""
vpn_accounting_probe.py — standalone diagnostic for the "does my VPN client cause
NetTally to double-count bytes?" question.

WHY THIS EXISTS
----------------
NetTally's collector.py attributes network bytes to whichever *process* nettop says
generated them (per-process socket-level counters via nettop -P). When a VPN client
is active, a downloaded file typically produces bytes on TWO different processes:

  1. The originating app (e.g. curl, a browser) — its own socket write/read counters,
     measured before the OS routes the traffic through the VPN's virtual tunnel
     interface (utunN on macOS).
  2. The VPN client's own process — which reads that same payload off the tunnel
     interface, encrypts/encapsulates it, and writes it out again over the real
     interface (en0/en1). That's a second socket, on a second process, carrying
     (approximately) the same payload.

If that's what's happening, summing "per-app bytes" across all apps (including the
VPN app) roughly DOUBLES the real number of bytes that left the machine. But that's
an architecture-dependent claim, not a universal one — some VPN implementations
(e.g. NEFilterProvider-style content filters, some corporate MDM VPNs) don't work
this way. It has to be measured on your actual VPN client, not assumed.

This script does NOT assume the answer. It gives you three independent
measurements for the same controlled download, done twice (VPN off, VPN on):

  A. curl's own report of bytes downloaded (control: what actually arrived at the
     HTTP layer).
  B. nettop per-process byte deltas (what NetTally itself would record) — folded
     through the repo's app_map.json the same way collector.py does.
  C. Interface-level byte deltas via `netstat -ib`, separated into "tunnel-like"
     interfaces (utunN / ipsecN / pppN / tunN / tapN) and "physical" interfaces
     (enN / awdlN / bridgeN).

(C) is the strongest evidence: if a utunN interface shows ~download-size bytes AND
a physical enN interface *also* shows ~download-size bytes (+ VPN overhead) for the
same download, that is direct proof the data crossed two interfaces — independent of
however nettop chooses to attribute it to processes. (B) then tells you specifically
whether NetTally's per-app numbers reproduce that duplication.

IMPORTANT — WHERE TO RUN THIS
------------------------------
This must run in a real Terminal on your Mac (nettop/netstat/curl are macOS/BSD
tools). It will not run inside a sandboxed shell that doesn't have those binaries.

USAGE
-----
    # 1. Make sure your VPN is OFF, then, ideally with the machine otherwise idle:
    python3 tools/vpn_accounting_probe.py trial --label vpn_off --bytes 100000000

    # 2. Turn your VPN ON, then run the *same* download again:
    python3 tools/vpn_accounting_probe.py trial --label vpn_on --bytes 100000000

    # Repeat both a few times (interleaved: off, on, off, on, ...) to average out
    # network noise — see the methodology notes below.

    # 3. Once you have a few trials of each, look at the per-process table printed
    # by each trial to find your VPN's actual process name, then:
    python3 tools/vpn_accounting_probe.py analyze --vpn-name "WireGuard"

METHODOLOGY NOTES (read before trusting the numbers)
------------------------------------------------------
- Interleave vpn_off/vpn_on trials rather than running all of one then all of the
  other — background network variance (Wi-Fi conditions, CDN routing) can drift
  over time and you don't want it to alias with the on/off condition.
- Quiet the machine during trials: pause iCloud/Dropbox/Backblaze/etc. sync, close
  apps that phone home. Background chatter is noise added to both conditions but
  it's cleaner if it's small.
- The download endpoint defaults to Cloudflare's public speed-test endpoint, which
  accepts an exact byte count and isn't cached — good for a repeatable, known-size
  payload. If your network blocks it, pass --url to point at another HTTPS
  file/endpoint with a stable, known size.
- A single trial is anecdote, not evidence — run at least 4-5 per condition before
  trusting the ratios `analyze` prints.
- This measures ONE VPN client's behavior. If you use more than one VPN app/mode,
  or a corporate MDM-pushed VPN profile, re-run the test under each — the answer is
  not guaranteed to generalize even between VPN products.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime

TRIAL_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vpn_accounting_trials.jsonl")

TUNNEL_RE = re.compile(r"^(utun|ipsec|ppp|tun|tap)\d*$")
PHYSICAL_RE = re.compile(r"^(en|awdl|bridge)\d*$")


# ---------------------------------------------------------------------------
# nettop sampling — mirrors collector.py's fetch_nettop_sample() parsing so
# the numbers this script sees match what NetTally's own daemon would record.
# ---------------------------------------------------------------------------
def nettop_sample():
    cmd = ["nettop", "-P", "-L", "1", "-x", "-J", "bytes_in,bytes_out"]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
    except FileNotFoundError:
        sys.exit("ERROR: `nettop` not found. This script must run in a Terminal on macOS, not a sandboxed shell.")
    samples = {}
    for line in res.stdout.strip().splitlines():
        line = line.strip()
        if not line or ("bytes_in" in line and "bytes_out" in line):
            continue
        parts = line.rstrip(",").rsplit(",", 2)
        if len(parts) < 3:
            continue
        proc_id, bi, bo = parts
        try:
            bi, bo = int(bi.strip()), int(bo.strip())
        except ValueError:
            continue
        proc_id = proc_id.strip()
        samples[proc_id] = {"bytes_in": bi, "bytes_out": bo}
    return samples


def nettop_delta(before, after):
    deltas = {}
    for key, a in after.items():
        b = before.get(key)
        if b is None:
            # Not present in the "before" snapshot -> this process didn't exist
            # yet when the window started. nettop's counters are cumulative
            # since the process launched, and we know it launched during our
            # window, so the raw cumulative value on our last live sample of it
            # IS the correct delta (there's nothing to subtract). Treating this
            # as "no reliable baseline, skip" (as collector.py does, to avoid
            # backdating a long-lived process's pre-monitoring history at
            # daemon startup) would silently drop short-lived processes like a
            # one-shot `curl` entirely — which is exactly what happened here.
            din, dout = a["bytes_in"], a["bytes_out"]
        else:
            din = a["bytes_in"] - b["bytes_in"]
            dout = a["bytes_out"] - b["bytes_out"]
            if din < 0 or dout < 0:
                continue  # pid reuse / counter reset
        if din or dout:
            deltas[key] = {"bytes_in": din, "bytes_out": dout}
    return deltas


# ---------------------------------------------------------------------------
# app-name folding — reuses the repo's app_map.json if it's found nearby, so
# folded names match what NetTally itself would show. Falls back to raw names.
# ---------------------------------------------------------------------------
def load_app_map():
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, "..", "app_map.json"), os.path.join(here, "app_map.json")):
        if os.path.exists(candidate):
            try:
                with open(candidate) as f:
                    return json.load(f)
            except Exception as e:
                print(f"warning: failed to load {candidate}: {e}", file=sys.stderr)
    return {}


def fold_name(raw_key, app_map):
    name = raw_key
    if "." in raw_key:
        head, tail = raw_key.rsplit(".", 1)
        if tail.isdigit():
            name = head
    exact = app_map.get("exact_map", {})
    if name in exact:
        return exact[name]
    cleaned = re.sub(r"\s+(Helper|\(Renderer\)|\(GPU\)|\(Plugin\)).*$", "", name, flags=re.IGNORECASE).strip()
    if cleaned in exact:
        return exact[cleaned]
    for prefix, target in app_map.get("prefix_map", {}).items():
        if name.startswith(prefix) or cleaned.startswith(prefix):
            return target
    return cleaned or name


# ---------------------------------------------------------------------------
# interface counters
# ---------------------------------------------------------------------------
def netstat_ib():
    try:
        res = subprocess.run(["netstat", "-ib"], stdout=subprocess.PIPE, text=True, timeout=15)
    except FileNotFoundError:
        sys.exit("ERROR: `netstat` not found. Run this in a macOS Terminal.")
    lines = res.stdout.strip().splitlines()
    if not lines:
        return {}
    header = lines[0].split()
    try:
        name_i = header.index("Name")
        ibytes_i = header.index("Ibytes")
        obytes_i = header.index("Obytes")
    except ValueError:
        return {}
    ifaces = {}
    for line in lines[1:]:
        cols = line.split()
        if len(cols) <= max(name_i, ibytes_i, obytes_i):
            continue
        name = cols[name_i]
        try:
            ib, ob = int(cols[ibytes_i]), int(cols[obytes_i])
        except ValueError:
            continue
        # netstat -ib prints one row per (interface, address family); keep the max
        # seen per interface (the link-layer row carries the real cumulative totals).
        prev_ib, prev_ob = ifaces.get(name, (0, 0))
        ifaces[name] = (max(prev_ib, ib), max(prev_ob, ob))
    return ifaces


def iface_delta(before, after):
    out = {}
    for name, (ib_a, ob_a) in after.items():
        ib_b, ob_b = before.get(name, (0, 0))
        din, dout = ib_a - ib_b, ob_a - ob_b
        if din < 0 or dout < 0:
            continue
        if din or dout:
            out[name] = {"ibytes": din, "obytes": dout}
    return out


# ---------------------------------------------------------------------------
# trial
# ---------------------------------------------------------------------------
POLL_INTERVAL = 0.25  # seconds between nettop samples while curl is running


def run_trial(args):
    print(f"[{args.label}] baseline snapshot...")
    nt_before = nettop_sample()
    if_before = netstat_ib()

    print(f"[{args.label}] downloading {args.bytes:,} bytes from {args.url} ...")
    t0 = time.time()
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{size_download},%{time_total},%{http_code}"]
    if args.range:
        # Slice an exact byte range out of a (larger) resource via HTTP Range,
        # for servers that don't support a `?bytes=N` query param of their own.
        cmd += ["-r", f"0-{args.bytes - 1}"]
    cmd += [args.url]

    # Run curl in the background and keep sampling nettop WHILE it's alive,
    # merging each sample's per-key byte counters into `live_sample` (later
    # samples overwrite earlier ones — nettop's counters are monotonic while
    # a process lives, so the last value seen for a key is its peak so far).
    # This matters because curl is typically a short-lived process: a single
    # nettop snapshot taken only *after* curl has already exited can miss it
    # entirely, since its PID is gone by then (absent from that snapshot, not
    # zero — nettop_delta then has nothing to compute a delta from). Sampling
    # continuously during the download, and using the merged running values as
    # the "after" state, avoids that race.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    live_sample = {}
    while proc.poll() is None:
        for key, val in nettop_sample().items():
            live_sample[key] = val
        time.sleep(POLL_INTERVAL)
    for key, val in nettop_sample().items():  # one last catch-up sample
        live_sample[key] = val
    try:
        stdout, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        stdout = ""
    elapsed = time.time() - t0

    nt_after = live_sample
    if_after = netstat_ib()

    try:
        size_dl_s, time_total_s, http_code = stdout.split(",")
        size_dl = int(size_dl_s)
    except Exception:
        print(f"WARNING: could not parse curl output: {stdout!r}", file=sys.stderr)
        size_dl, http_code = 0, "?"
    http_code = http_code.strip() if isinstance(http_code, str) else http_code

    # Sanity check BEFORE trusting anything downstream: a blocked/redirected/
    # error response (a 403 page, a captive-portal redirect, a corporate proxy
    # block page) is often small and returns a "successful" curl exit code —
    # it will silently poison the whole comparison if logged as if it were a
    # real download. This bit us: a corporate VPN blocked the default test
    # endpoint outright and every trial logged noise instead of a real transfer.
    download_ok = http_code in ("200", "206") and size_dl >= 0.9 * args.bytes
    if not download_ok and not args.force:
        print(f"\nERROR: download did not look like a real {args.bytes:,}-byte transfer.")
        print(f"  http_code={http_code}  size_download={size_dl:,}  elapsed={elapsed:.2f}s")
        print("  This almost always means the URL is blocked/altered by a network policy")
        print("  (corporate proxy, VPN URL filtering, captive portal) rather than a real")
        print("  measurement — logging it would poison your comparison, so this trial was")
        print("  NOT recorded.")
        print(f'  Try: python3 {sys.argv[0]} check-url "{args.url}" [other candidate URLs...]')
        print("  to find a URL your network actually allows, then re-run with --url.")
        print("  (Pass --force to log it anyway, e.g. for debugging.)")
        return

    proc_deltas = nettop_delta(nt_before, nt_after)
    ifc_deltas = iface_delta(if_before, if_after)
    app_map = load_app_map()

    folded = {}
    for key, d in proc_deltas.items():
        app = fold_name(key, app_map)
        f = folded.setdefault(app, {"bytes_in": 0, "bytes_out": 0, "raw_keys": []})
        f["bytes_in"] += d["bytes_in"]
        f["bytes_out"] += d["bytes_out"]
        f["raw_keys"].append(key)

    trial = {
        "trial_id": str(uuid.uuid4())[:8],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "label": args.label,
        "requested_bytes": args.bytes,
        "curl_size_download": size_dl,
        "curl_http_code": http_code,
        "elapsed_s": round(elapsed, 2),
        "url": args.url,
        "process_deltas": folded,
        "interface_deltas": ifc_deltas,
    }

    with open(args.out, "a") as f:
        f.write(json.dumps(trial) + "\n")

    print(f"\n--- trial {trial['trial_id']} ({args.label}) ---")
    print(f"curl reported download: {size_dl:,} bytes ({trial['curl_http_code']}) in {elapsed:.1f}s")

    print("\nper-process bytes (nettop, folded, nonzero only, top 15):")
    for app, d in sorted(folded.items(), key=lambda kv: -(kv[1]["bytes_in"] + kv[1]["bytes_out"]))[:15]:
        total = d["bytes_in"] + d["bytes_out"]
        print(f"  {app:<30} in={d['bytes_in']:>12,}  out={d['bytes_out']:>12,}  total={total:>12,}")

    print("\ninterface bytes (netstat -ib, nonzero only):")
    for name, d in sorted(ifc_deltas.items(), key=lambda kv: -(kv[1]["ibytes"] + kv[1]["obytes"])):
        total = d["ibytes"] + d["obytes"]
        tag = " (tunnel-like)" if TUNNEL_RE.match(name) else (" (physical)" if PHYSICAL_RE.match(name) else "")
        print(f"  {name:<10} in={d['ibytes']:>12,} out={d['obytes']:>12,} total={total:>12,}{tag}")

    print(f"\nlogged to {args.out}")
    if args.label == "vpn_on":
        print("\nTip: find your VPN's process name above (or in Activity Monitor's Network tab),")
        print("     then pass it to `analyze --vpn-name \"...\"` next.")


# ---------------------------------------------------------------------------
# check-url — quickly test candidate download URLs against your actual
# network/VPN policy before burning real trials on one that's blocked.
# ---------------------------------------------------------------------------
DEFAULT_CANDIDATES = [
    "https://speed.cloudflare.com/__down?bytes=5000000",
    "https://ash-speed.hetzner.com/100MB.bin",
    "https://proof.ovh.net/files/100Mb.dat",
    # GitHub Releases CDN — often allowed even on locked-down corporate networks
    # since it's needed for everyday dev tooling. Use with --range on `trial`
    # since it has no ?bytes= param of its own.
    "https://github.com/git-for-windows/git/releases/download/v2.45.2.windows.1/Git-2.45.2-64-bit.exe",
]


def run_check(args):
    urls = args.urls or DEFAULT_CANDIDATES
    print(f"Testing {len(urls)} candidate URL(s) with a small ({args.bytes:,}-byte) request...\n")
    for url in urls:
        cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code},%{size_download},%{time_total}",
               "-r", f"0-{args.bytes - 1}", url]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, timeout=args.timeout)
            http_code, size_dl, time_total = res.stdout.split(",")
            size_dl = int(size_dl)
            ok = http_code in ("200", "206") and size_dl >= 0.9 * args.bytes
            verdict = "OK — looks like a real transfer" if ok else "BLOCKED/UNRELIABLE — do not use"
            print(f"[{verdict}] {url}")
            print(f"    http_code={http_code} size_download={size_dl:,} time={float(time_total):.2f}s")
        except Exception as e:
            print(f"[ERROR] {url}\n    {e}")
    print("\nUse a URL marked OK with `trial --url ... --range` (add --range if it came from")
    print("this candidate list without a ?bytes= parameter of its own, e.g. the GitHub one).")


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------
def run_analyze(args):
    if not os.path.exists(args.log):
        sys.exit(f"No trial log at {args.log}. Run some `trial` commands first.")
    trials = [json.loads(l) for l in open(args.log) if l.strip()]
    if args.label:
        trials = [t for t in trials if t["label"] == args.label]
    if not trials:
        sys.exit("No trials match.")

    by_label = {}
    for t in trials:
        by_label.setdefault(t["label"], []).append(t)

    def app_bytes(t, name_substr):
        return sum(
            d["bytes_in"] + d["bytes_out"]
            for app, d in t["process_deltas"].items()
            if name_substr.lower() in app.lower()
        )

    def iface_bytes(t, pattern):
        return sum(
            d["ibytes"] + d["obytes"]
            for name, d in t["interface_deltas"].items()
            if pattern.match(name)
        )

    header = f"{'label':<10} {'n':<3} {'req_MB':<8} {'app_MB':<8} {'vpn_MB':<8} {'phys_MB':<8} {'tun_MB*':<8} {'(app+vpn)/phys':<15} {'vpn/phys':<9}"
    print(header)
    print("-" * len(header))
    summary = {}
    for label, ts in by_label.items():
        n = len(ts)
        req = statistics.mean(t["requested_bytes"] for t in ts) / 1e6
        app_vals = [app_bytes(t, args.app_name) for t in ts]
        vpn_vals = [app_bytes(t, args.vpn_name) for t in ts] if args.vpn_name else [0.0] * n
        phys_vals = [iface_bytes(t, PHYSICAL_RE) for t in ts]
        tun_vals = [iface_bytes(t, TUNNEL_RE) for t in ts]

        app_m, vpn_m = statistics.mean(app_vals) / 1e6, statistics.mean(vpn_vals) / 1e6
        # phys_MB and vpn_MB are the two trustworthy quantities here -- they've
        # been empirically cross-checked against each other and against known
        # request sizes and agree closely. tun_MB (bytes on utun-style tunnel
        # interfaces, from netstat -ib) is NOT trustworthy: measured on macOS
        # with GlobalProtect, it consistently ran ~1.8-1.9x higher than BOTH
        # phys_MB and vpn_MB for the same transfer -- a virtual point-to-point
        # tunnel interface appears to double-count relative to a physical NIC
        # (plausibly because the same packet gets tallied once handed from the
        # kernel routing layer to the tunnel's userspace socket, and again as
        # the VPN client reads/acks it through that same socket -- unconfirmed
        # mechanism, confirmed empirical effect). Keep it visible for
        # diagnostic curiosity but never use it as a ground-truth number.
        phys_m, tun_m = statistics.mean(phys_vals) / 1e6, statistics.mean(tun_vals) / 1e6
        ratio_doublecount = (app_m + vpn_m) / phys_m if phys_m else float("nan")
        ratio_vpn_vs_phys = vpn_m / phys_m if phys_m else float("nan")

        summary[label] = dict(app_m=app_m, vpn_m=vpn_m, phys_m=phys_m, tun_m=tun_m,
                               ratio_doublecount=ratio_doublecount, ratio_vpn_vs_phys=ratio_vpn_vs_phys)
        print(f"{label:<10} {n:<3} {req:<8.1f} {app_m:<8.1f} {vpn_m:<8.1f} {phys_m:<8.1f} {tun_m:<8.1f} "
              f"{ratio_doublecount:<15.2f} {ratio_vpn_vs_phys:<9.2f}")

    print("\nColumn key:")
    print("  app_MB   = bytes nettop attributed to the process matching --app-name (default 'curl')")
    print("  vpn_MB   = bytes nettop attributed to the process matching --vpn-name (0 if not supplied)")
    print("  phys_MB  = bytes on physical interfaces (enN/awdlN/bridgeN) — the reliable 'real usage' number")
    print("  tun_MB*  = bytes on tunnel-like interfaces (utunN/ipsecN/pppN/...) — ** UNRELIABLE, do not use **")
    print("             as evidence. Observed running ~1.8-1.9x higher than phys_MB and vpn_MB for the same")
    print("             transfer (macOS utun interface counters appear to double-count internally). Shown only")
    print("             for diagnostic curiosity, not as a ground-truth quantity.")
    print("  (app+vpn)/phys ~ 2.0  => summing per-app totals (incl. VPN) roughly doubles real bytes: confirmed")
    print("  (app+vpn)/phys ~ 1.0  => no double counting for this app+VPN combination")
    print("  vpn/phys       ~ 1.0  => the VPN process's own bytes alone already track real usage closely —")
    print("                           meaning if you want an accurate TOTAL, the fix is to trust VPN bytes and")
    print("                           drop the other per-app rows while it's active, not the reverse.")
    if not args.vpn_name:
        print("\n(Pass --vpn-name \"<substring>\" to fill in the vpn_MB / ratio columns — check a vpn_on trial's")
        print(" printed process table, or Activity Monitor's Network tab, for your VPN client's process name.)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("trial", help="run one controlled download and record nettop + interface deltas")
    t.add_argument("--label", required=True, help="e.g. vpn_off / vpn_on — you set the VPN state manually before running")
    t.add_argument("--bytes", type=int, default=100_000_000, help="payload size to request (default 100,000,000 = ~100MB)")
    t.add_argument("--url", default=None, help="override download URL (default: Cloudflare speed-test endpoint sized to --bytes)")
    t.add_argument("--out", default=TRIAL_LOG, help="jsonl log file to append to")
    t.add_argument("--timeout", type=int, default=180)
    t.add_argument("--range", action="store_true",
                    help="use HTTP Range to slice --bytes out of --url instead of a ?bytes= query param")
    t.add_argument("--force", action="store_true",
                    help="log the trial even if the download doesn't look like a real transfer (debugging only)")
    t.set_defaults(func=run_trial)

    c = sub.add_parser("check-url", help="test candidate URLs against your network/VPN policy before running trials")
    c.add_argument("urls", nargs="*", help="URLs to test (default: a built-in candidate list)")
    c.add_argument("--bytes", type=int, default=5_000_000, help="test request size (default 5MB)")
    c.add_argument("--timeout", type=int, default=60)
    c.set_defaults(func=run_check)

    a = sub.add_parser("analyze", help="summarize a trial log")
    a.add_argument("--log", default=TRIAL_LOG)
    a.add_argument("--label", default=None, help="filter to one label only")
    a.add_argument("--app-name", default="curl", help="substring matching the downloading process (default: curl)")
    a.add_argument("--vpn-name", default=None, help="substring matching your VPN's process name")
    a.set_defaults(func=run_analyze)

    args = p.parse_args()
    if args.cmd == "trial" and args.url is None:
        args.url = f"https://speed.cloudflare.com/__down?bytes={args.bytes}"
    args.func(args)


if __name__ == "__main__":
    main()
