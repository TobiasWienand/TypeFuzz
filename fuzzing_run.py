import argparse
import json
import os
import signal
import subprocess
import sys
import time

IMAGE_DEFAULT = "typefuzz-eval"
PORT_DEFAULT = 1337
STATS_INTERVAL_DEFAULT = 10
MONITOR_INTERVAL = 300
MAX_PHYSICAL_CORES = 256
D8_PATH = "/work/v8/out/fuzzbuild/d8"
FUZZILLI_BIN = "FuzzilliCli"


def generate_session_id():
    rand = os.urandom(4).hex()
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{rand}"


def create_output_dirs(base, num_workers):
    os.makedirs(f"{base}/root", exist_ok=True)
    os.makedirs(f"{base}/logs", exist_ok=True)
    for i in range(num_workers):
        os.makedirs(f"{base}/leaf_{i}", exist_ok=True)


def write_metadata(base, args, session_id, core_map):
    meta = {
        "session_id": session_id,
        "start_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "feedback_mode": args.feedback_mode,
        "num_workers": args.num_workers,
        "cores_per_worker": args.cores_per_worker,
        "start_core": args.start_core,
        "duration_hours": args.duration,
        "image": args.image,
        "port": args.port,
        "stats_interval_min": args.stats_interval,
        "total_cores": 1 + args.num_workers * args.cores_per_worker,
        "core_map": core_map,
    }
    with open(f"{base}/metadata.json", "w") as f:
        json.dump(meta, f, indent=2)


def container_name(mode, role, session_id):
    return f"tf-{mode}-{role}-{session_id}"


def launch_root(args, session_id, base):
    name = container_name(args.feedback_mode, "root", session_id)
    log = open(f"{base}/logs/root.log", "w")
    core = str(args.start_core)
    cmd = [
        "docker", "run", "-d", "--rm",
        "--name", name,
        "--network=host",
        "--cpuset-cpus", core,
        "--shm-size=256m",
        "-v", f"{os.path.abspath(base)}/root:/data",
        args.image,
        FUZZILLI_BIN,
        "--profile=v8",
        "--jobs=1",
        "--instanceType=root",
        f"--bindTo=127.0.0.1:{args.port}",
        "--storagePath=/data",
        "--exportStatistics",
        f"--statisticsExportInterval={args.stats_interval}",
        f"--feedback-mode={args.feedback_mode}",
        f"--maxRuntimeInHours={int(args.duration)}",
        D8_PATH,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: failed to launch root container")
        print(result.stderr)
        sys.exit(1)

    cid = result.stdout.strip()[:12]
    # Redirect container logs to file
    subprocess.Popen(
        ["docker", "logs", "-f", name],
        stdout=log, stderr=subprocess.STDOUT,
    )
    return name, cid


def launch_leaf(args, session_id, base, leaf_idx, core_start, core_end):
    name = container_name(args.feedback_mode, f"leaf{leaf_idx}", session_id)
    log = open(f"{base}/logs/leaf_{leaf_idx}.log", "w")
    cmd = [
        "docker", "run", "-d", "--rm",
        "--name", name,
        "--network=host",
        "--cpuset-cpus", f"{core_start}-{core_end}",
        "--shm-size=256m",
        "-v", f"{os.path.abspath(base)}/leaf_{leaf_idx}:/data",
        args.image,
        FUZZILLI_BIN,
        "--profile=v8",
        f"--jobs={args.cores_per_worker}",
        "--instanceType=leaf",
        f"--connectTo=127.0.0.1:{args.port}",
        "--storagePath=/data",
        "--exportStatistics",
        f"--statisticsExportInterval={args.stats_interval}",
        f"--feedback-mode={args.feedback_mode}",
        f"--maxRuntimeInHours={int(args.duration)}",
        "--logLevel=warning",
        D8_PATH,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: failed to launch leaf {leaf_idx}")
        print(result.stderr)
        return None

    cid = result.stdout.strip()[:12]
    subprocess.Popen(
        ["docker", "logs", "-f", name],
        stdout=log, stderr=subprocess.STDOUT,
    )
    return name


def is_alive(name):
    result = subprocess.run(
        ["docker", "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    return name in result.stdout


def read_temps():
    try:
        result = subprocess.run(
            ["sensors"], capture_output=True, text=True, timeout=5,
        )
        temps = []
        for line in result.stdout.splitlines():
            if "Tctl" in line:
                temps.append(line.split(":")[1].strip().split()[0])
        return " ".join(temps) if temps else "n/a"
    except Exception:
        return "n/a"


def monitor(args, session_id, container_names, base):
    start = time.time()
    expected = args.duration * 3600
    min_duration = expected * 0.8

    print(f"\n[{time.strftime('%H:%M:%S')}] Monitoring {len(container_names)} "
          f"containers. Expected completion: "
          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(start + expected))}")

    while True:
        time.sleep(MONITOR_INTERVAL)
        alive = sum(1 for n in container_names if is_alive(n))
        temps = read_temps()
        root_status = "UP" if is_alive(container_names[0]) else "DOWN"
        print(f"[{time.strftime('%H:%M:%S')}] {alive}/{len(container_names)} "
              f"alive | root:{root_status} | temps: {temps}")

        if alive == 0:
            elapsed = time.time() - start
            if elapsed < min_duration:
                print(f"ERROR: all containers died after {elapsed/3600:.1f}h "
                      f"(expected {args.duration}h)")
                with open(f"{base}/FAILED", "w") as f:
                    f.write(f"early termination at {elapsed/3600:.1f}h\n")
            else:
                print(f"All containers exited after {elapsed/3600:.1f}h")
            break


def cleanup(session_id, mode):
    result = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"name=tf-{mode}"],
        capture_output=True, text=True,
    )
    cids = result.stdout.strip().split()
    if cids:
        print(f"Stopping {len(cids)} containers...")
        subprocess.run(["docker", "stop"] + cids, capture_output=True)
        time.sleep(5)
    stale = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"name=tf-{mode}"],
        capture_output=True, text=True,
    )
    if stale.stdout.strip():
        subprocess.run(
            ["docker", "kill"] + stale.stdout.strip().split(),
            capture_output=True,
        )


def print_summary(base, num_workers):
    print(f"\n{'='*50}")
    print("SUMMARY")
    print(f"{'='*50}")
    for role in ["root"] + [f"leaf_{i}" for i in range(num_workers)]:
        d = f"{base}/{role}"
        stats = len([f for f in os.listdir(f"{d}/stats") if f.endswith(".json")]) if os.path.isdir(f"{d}/stats") else 0
        corpus = len(os.listdir(f"{d}/corpus")) if os.path.isdir(f"{d}/corpus") else 0
        crashes = len([f for f in os.listdir(f"{d}/crashes") if f.endswith(".js")]) if os.path.isdir(f"{d}/crashes") else 0
        print(f"  {role:10s}: {stats:4d} stat snapshots, {corpus:5d} corpus, {crashes:3d} crashes")
    print(f"{'='*50}")
    print(f"Output: {base}")


def main():
    parser = argparse.ArgumentParser(description="Run a fuzzing campaign")
    parser.add_argument("--feedback-mode", required=True,
                        choices=["code", "type", "hybrid"])
    parser.add_argument("--num-workers", required=True, type=int)
    parser.add_argument("--cores-per-worker", required=True, type=int)
    parser.add_argument("--start-core", required=True, type=int)
    parser.add_argument("--duration", required=True, type=float,
                        help="Duration in hours")
    parser.add_argument("--image", default=IMAGE_DEFAULT)
    parser.add_argument("--port", type=int, default=PORT_DEFAULT)
    parser.add_argument("--stats-interval", type=int, default=STATS_INTERVAL_DEFAULT,
                        help="Minutes between stats exports")
    parser.add_argument("--output-dir", default="data/campaign")
    parser.add_argument("--session-id", default=None)
    args = parser.parse_args()

    total_cores = 1 + args.num_workers * args.cores_per_worker
    last_core = args.start_core + total_cores - 1
    if last_core >= MAX_PHYSICAL_CORES:
        print(f"ERROR: cores {args.start_core}-{last_core} exceeds "
              f"{MAX_PHYSICAL_CORES} physical cores")
        sys.exit(1)

    session_id = args.session_id or generate_session_id()
    base = f"{args.output_dir}/{session_id}"

    core_map = {"root": args.start_core}
    leaf_start = args.start_core + 1
    for i in range(args.num_workers):
        s = leaf_start + i * args.cores_per_worker
        e = s + args.cores_per_worker - 1
        core_map[f"leaf_{i}"] = f"{s}-{e}"

    print(f"Campaign: {args.feedback_mode} mode, {args.num_workers} workers "
          f"x {args.cores_per_worker} cores, {args.duration}h")
    print(f"Session:  {session_id}")
    print(f"Cores:    {args.start_core}-{last_core} ({total_cores} total)")
    print(f"Image:    {args.image}")
    print(f"Output:   {base}")
    print()

    create_output_dirs(base, args.num_workers)
    write_metadata(base, args, session_id, core_map)

    cleanup(session_id, args.feedback_mode)

    caught_signal = []

    def on_signal(signum, frame):
        caught_signal.append(signum)
        print(f"\nReceived signal {signum}, cleaning up...")
        cleanup(session_id, args.feedback_mode)
        sys.exit(1)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    print("Launching root...")
    root_name, root_cid = launch_root(args, session_id, base)
    print(f"  {root_name} on core {args.start_core}")

    time.sleep(10)

    if not is_alive(root_name):
        print("FATAL: root container failed to start.")
        subprocess.run(["docker", "logs", root_name], check=False)
        sys.exit(1)

    container_names = [root_name]
    print(f"Launching {args.num_workers} leaves...")
    for i in range(args.num_workers):
        s = leaf_start + i * args.cores_per_worker
        e = s + args.cores_per_worker - 1
        name = launch_leaf(args, session_id, base, i, s, e)
        if name:
            container_names.append(name)
            print(f"  {name} on cores {s}-{e}")
        else:
            print(f"  WARNING: leaf {i} failed to launch")

    print(f"\n{len(container_names)} containers launched.")

    monitor(args, session_id, container_names, base)
    cleanup(session_id, args.feedback_mode)
    print_summary(base, args.num_workers)


if __name__ == "__main__":
    main()
