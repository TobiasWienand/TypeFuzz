import argparse
import json
import os
import signal
import subprocess
import sys
import time

MAX_PHYSICAL_CORES = 256
PORT_BASE = 1337
STATS_INTERVAL_DEFAULT = 10
IMAGE_DEFAULT = "typefuzz-eval"


def generate_session_id():
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(4).hex()}"


def run_fuzzing(mode, rep, slot, cores_per_config, num_workers, cores_per_worker,
                duration, image, stats_interval, base_dir):
    start_core = slot * cores_per_config
    port = PORT_BASE + slot
    output_dir = f"{base_dir}/{mode}_rep{rep}"

    cmd = [
        sys.executable, "fuzzing_run.py",
        "--feedback-mode", mode,
        "--num-workers", str(num_workers),
        "--cores-per-worker", str(cores_per_worker),
        "--start-core", str(start_core),
        "--duration", str(duration),
        "--image", image,
        "--port", str(port),
        "--stats-interval", str(stats_interval),
        "--output-dir", output_dir,
    ]
    log_path = f"{base_dir}/logs/{mode}_rep{rep}.log"
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    return proc, log_path


def wait_for_slice(procs, run_labels, duration_hours):
    start = time.time()
    while True:
        alive = [(l, p) for l, p in zip(run_labels, procs) if p.poll() is None]
        if not alive:
            break
        time.sleep(60)
        elapsed = time.time() - start
        status = " ".join(f"{l}:{'UP' if p.poll() is None else 'DONE'}"
                          for l, p in zip(run_labels, procs))
        print(f"  [{time.strftime('%H:%M')}] {len(alive)}/{len(procs)} alive | "
              f"{status} | {elapsed/3600:.1f}h/{duration_hours}h")

    for p in procs:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()


def cleanup_all():
    result = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=tf-"],
        capture_output=True, text=True)
    cids = result.stdout.strip().split()
    if cids and cids[0]:
        subprocess.run(["docker", "stop", "-t", "10"] + cids, capture_output=True)
        time.sleep(3)
        stale = subprocess.run(
            ["docker", "ps", "-q", "--filter", "name=tf-"],
            capture_output=True, text=True)
        if stale.stdout.strip():
            subprocess.run(["docker", "kill"] + stale.stdout.strip().split(),
                           capture_output=True)


def print_run_summary(base_dir, label, num_workers):
    run_dir = f"{base_dir}/{label}"
    # fuzzing_run.py creates a session subdir inside output_dir
    session_dirs = [d for d in os.listdir(run_dir)
                    if os.path.isdir(f"{run_dir}/{d}")] if os.path.isdir(run_dir) else []
    if not session_dirs:
        print(f"  {label:16s}: NO DATA")
        return
    sd = f"{run_dir}/{session_dirs[0]}"
    stats = len([f for f in os.listdir(f"{sd}/root/stats")
                 if f.endswith(".json")]) if os.path.isdir(f"{sd}/root/stats") else 0
    corpus = len(os.listdir(f"{sd}/root/corpus")) if os.path.isdir(f"{sd}/root/corpus") else 0
    crashes = 0
    for w in ["root"] + [f"leaf_{i}" for i in range(num_workers)]:
        cdir = f"{sd}/{w}/crashes"
        if os.path.isdir(cdir):
            crashes += sum(1 for f in os.listdir(cdir) if f.endswith(".js"))
    print(f"  {label:16s}: {stats:4d} snapshots, {corpus:5d} corpus, {crashes:3d} crashes")


def main():
    parser = argparse.ArgumentParser(
        description="Run a multi-config, multi-rep fuzzing campaign with scheduling"
    )
    parser.add_argument("--duration", required=True, type=int,
                        help="Duration per run in hours (integer)")
    parser.add_argument("--num-workers", required=True, type=int)
    parser.add_argument("--cores-per-worker", required=True, type=int)
    parser.add_argument("--feedback-modes", required=True,
                        help="Comma-separated: type,code,hybrid")
    parser.add_argument("--num-reps", required=True, type=int)
    parser.add_argument("--image", default=IMAGE_DEFAULT)
    parser.add_argument("--stats-interval", type=int, default=STATS_INTERVAL_DEFAULT)
    parser.add_argument("--output-dir", default="data/campaign")
    args = parser.parse_args()

    modes = [m.strip() for m in args.feedback_modes.split(",")]
    for m in modes:
        if m not in ("type", "code", "hybrid"):
            print(f"ERROR: invalid feedback mode '{m}'")
            sys.exit(1)

    cores_per_config = 1 + args.num_workers * args.cores_per_worker
    runs_per_slice = MAX_PHYSICAL_CORES // cores_per_config
    if runs_per_slice < 1:
        print(f"ERROR: {cores_per_config} cores per run exceeds "
              f"{MAX_PHYSICAL_CORES} available")
        sys.exit(1)

    # Build flat list of all runs
    all_runs = []
    for mode in modes:
        for rep in range(args.num_reps):
            all_runs.append((mode, rep))

    # Chunk into time slices
    slices = []
    for i in range(0, len(all_runs), runs_per_slice):
        slices.append(all_runs[i:i + runs_per_slice])

    session_id = generate_session_id()
    base_dir = f"{args.output_dir}/{session_id}"
    os.makedirs(base_dir, exist_ok=True)

    metadata = {
        "session_id": session_id,
        "start_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_hours": args.duration,
        "num_reps": args.num_reps,
        "feedback_modes": modes,
        "num_workers": args.num_workers,
        "cores_per_worker": args.cores_per_worker,
        "cores_per_config": cores_per_config,
        "runs_per_slice": runs_per_slice,
        "total_runs": len(all_runs),
        "total_slices": len(slices),
        "image": args.image,
        "schedule": [[f"{m}_rep{r}" for m, r in s] for s in slices],
    }
    with open(f"{base_dir}/metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    print("=" * 60)
    print("Fuzzing Campaign")
    print("=" * 60)
    print(f"  Session:       {session_id}")
    print(f"  Configs:       {', '.join(modes)}")
    print(f"  Reps:          {args.num_reps}")
    print(f"  Total runs:    {len(all_runs)}")
    print(f"  Per run:       {cores_per_config} cores "
          f"(1 root + {args.num_workers} x {args.cores_per_worker})")
    print(f"  Runs/slice:    {runs_per_slice} "
          f"({runs_per_slice * cores_per_config}/{MAX_PHYSICAL_CORES} cores)")
    print(f"  Time slices:   {len(slices)}")
    print(f"  Wall time:     ~{len(slices) * args.duration}h "
          f"({len(slices)} slices x {args.duration}h)")
    print()
    print("  Schedule:")
    for i, s in enumerate(slices):
        labels = [f"{m}_rep{r}" for m, r in s]
        print(f"    Slice {i+1:2d}: {', '.join(labels)}")
    print("=" * 60)
    print()

    def on_signal(signum, frame):
        print(f"\nSignal {signum}, cleaning up...")
        cleanup_all()
        sys.exit(1)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    for slice_idx, slice_runs in enumerate(slices):
        print(f"\n{'='*60}")
        print(f"SLICE {slice_idx+1} / {len(slices)}")
        print(f"{'='*60}")

        cleanup_all()

        procs = []
        labels = []
        for slot, (mode, rep) in enumerate(slice_runs):
            label = f"{mode}_rep{rep}"
            proc, log = run_fuzzing(
                mode, rep, slot, cores_per_config,
                args.num_workers, args.cores_per_worker,
                args.duration, args.image, args.stats_interval, base_dir)
            s = slot * cores_per_config
            e = s + cores_per_config - 1
            print(f"  {label}: cores {s}-{e}, port {PORT_BASE + slot} "
                  f"(pid {proc.pid})")
            procs.append(proc)
            labels.append(label)

        wait_for_slice(procs, labels, args.duration)
        cleanup_all()

        for label in labels:
            print_run_summary(base_dir, label, args.num_workers)

    print(f"\n{'='*60}")
    print(f"CAMPAIGN COMPLETE ({len(all_runs)} runs across {len(slices)} slices)")
    print(f"Output: {base_dir}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
