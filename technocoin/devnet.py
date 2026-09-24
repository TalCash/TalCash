"""`tc devnet`: a whole local TechnoCoin network on this computer.

Starts N devnet nodes as separate processes, each with its own data folder and
port (64187, 64188, ...), every node connected to every other, and the first M
of them mining. All share one genesis block. Their output is shown together,
each line tagged with the node's name. Ctrl+C stops them all.
"""

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .core.params import DEVNET
from .crypto import keys
from .crypto.address import encode_address, payload_from_public_key
from .node.network import GENESIS_FILE, load_params
from .paths import network_dir

FIRST_PORT = DEVNET.default_port


def cluster_dir(base: Path) -> Path:
    return base / "devnet-cluster"


def _relay_output(process: subprocess.Popen, name: str) -> None:
    for line in process.stdout:
        print(f"[{name}] {line.rstrip()}", flush=True)


def _stop(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)  # graceful stop for its own process group
            else:
                process.send_signal(signal.SIGINT)
    deadline = time.monotonic() + 15
    for process in processes:
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()


def run_devnet(
    base: Path,
    *,
    nodes: int = 3,
    miners: int = 2,
    threads: int = 2,
    mine_to: str | None = None,
    seconds: float | None = None,
    reset: bool = False,
) -> None:
    folder = cluster_dir(base)
    if reset and folder.exists():
        shutil.rmtree(folder)
    node_dirs = [folder / f"node{i}" for i in range(1, nodes + 1)]

    # One genesis for everyone: created in node1's folder, copied to the others.
    load_params(DEVNET.name, node_dirs[0])
    genesis_file = network_dir(DEVNET.name, node_dirs[0]) / GENESIS_FILE
    for node_dir in node_dirs[1:]:
        target = network_dir(DEVNET.name, node_dir) / GENESIS_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(genesis_file, target)

    ports = [FIRST_PORT + i for i in range(nodes)]
    print(f"Starting {nodes} devnet nodes ({min(miners, nodes)} mining) in {folder}", flush=True)
    processes: list[subprocess.Popen] = []
    try:
        for i, (node_dir, port) in enumerate(zip(node_dirs, ports)):
            command = [sys.executable, "-u", "-m", "technocoin", "--network", "devnet", "--datadir", str(node_dir),
                       "node", "--port", str(port)]
            for other in ports[:i]:
                command += ["--peer", f"ws://127.0.0.1:{other}/v1/p2p"]
            if i < miners:
                address = mine_to or encode_address(
                    payload_from_public_key(keys.public_key(keys.generate_private_key())), DEVNET.address_prefix)
                command += ["--mine", address, "--threads", str(threads)]
            name = f"node{i + 1}"
            print(f"[{name}] API http://127.0.0.1:{port}/v1/status" + (" (mining)" if i < miners else ""), flush=True)
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            processes.append(process)
            threading.Thread(target=_relay_output, args=(process, name), daemon=True).start()

        deadline = time.monotonic() + seconds if seconds else None
        while all(p.poll() is None for p in processes):
            if deadline and time.monotonic() > deadline:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping the devnet...", flush=True)
        _stop(processes)
