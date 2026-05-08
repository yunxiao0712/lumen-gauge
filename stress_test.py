#!/usr/bin/env python3
import argparse
import math
import multiprocessing as mp
import os
import signal
import subprocess
import time


def cpu_worker(stop_event: mp.Event) -> None:
    value = 0.001
    while not stop_event.is_set():
        for index in range(80_000):
            value += math.sin(index + value) * math.sqrt((index % 997) + 1)
        if value > 1_000_000:
            value = 0.001


def run_cpu(duration: int, workers: int) -> None:
    stop_event = mp.Event()
    processes = [mp.Process(target=cpu_worker, args=(stop_event,)) for _ in range(workers)]
    for process in processes:
        process.start()
    try:
        wait_with_progress(duration, f"CPU stress running with {workers} workers")
    finally:
        stop_event.set()
        for process in processes:
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
        print("CPU stress stopped")


def run_gpu(duration: int, matrix_size: int) -> None:
    try:
        import torch
    except ImportError:
        print_gpu_dependency_help()
        return

    if not torch.cuda.is_available():
        print("No CUDA GPU is available to PyTorch")
        return

    device = torch.device("cuda")
    size = matrix_size
    while size >= 512:
        try:
            a = torch.randn((size, size), device=device)
            b = torch.randn((size, size), device=device)
            break
        except RuntimeError as exc:
            if "out of memory" not in str(exc).lower():
                raise
            torch.cuda.empty_cache()
            size //= 2
    else:
        print("GPU stress could not allocate a CUDA matrix")
        return

    print(f"GPU stress running on {torch.cuda.get_device_name(0)} with {size}x{size} matrices")
    deadline = time.monotonic() + duration
    try:
        while time.monotonic() < deadline:
            c = a @ b
            a = b @ c
            b = c
            torch.cuda.synchronize()
            if time.monotonic() % 5 < 0.2:
                remaining = max(0, int(deadline - time.monotonic()))
                print(f"GPU stress: {remaining}s remaining", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        torch.cuda.empty_cache()
        print("GPU stress stopped")


def print_gpu_dependency_help() -> None:
    cuda_version = detect_cuda_version()
    if cuda_version and cuda_version.startswith("13."):
        index_url = "https://download.pytorch.org/whl/cu130"
    elif cuda_version and cuda_version.startswith("12.8"):
        index_url = "https://download.pytorch.org/whl/cu128"
    elif cuda_version and cuda_version.startswith("12.6"):
        index_url = "https://download.pytorch.org/whl/cu126"
    else:
        index_url = "https://download.pytorch.org/whl/cu128"

    print("GPU stress needs CUDA-enabled PyTorch.")
    if cuda_version:
        print(f"nvidia-smi reports CUDA {cuda_version}.")
    print("Install it in a project virtual environment:")
    print("  python3 -m venv .venv")
    print("  . .venv/bin/activate")
    print("  python3 -m pip install --upgrade pip")
    print(f"  python3 -m pip install torch --index-url {index_url}")
    print("Then run:")
    print("  .venv/bin/python stress_test.py --target gpu --duration 60")


def detect_cuda_version() -> str | None:
    try:
        version_result = subprocess.run(
            ["nvidia-smi"],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    match = re_search_cuda(version_result.stdout)
    return match


def re_search_cuda(text: str) -> str | None:
    marker = "CUDA Version:"
    index = text.find(marker)
    if index == -1:
        return None
    value = text[index + len(marker) :].strip().split()[0]
    return value if value else None


def wait_with_progress(duration: int, label: str) -> None:
    deadline = time.monotonic() + duration
    print(f"{label} for {duration}s. Press Ctrl+C to stop early.")
    try:
        while time.monotonic() < deadline:
            remaining = max(0, int(deadline - time.monotonic()))
            print(f"{remaining}s remaining", flush=True)
            time.sleep(min(5, max(1, remaining)))
    except KeyboardInterrupt:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Small bounded CPU/GPU stress test for Lumen Gauge.")
    parser.add_argument("--target", choices=("cpu", "gpu", "both"), default="cpu")
    parser.add_argument("--duration", type=int, default=60, help="Run time in seconds.")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1, help="CPU worker processes.")
    parser.add_argument("--gpu-size", type=int, default=4096, help="CUDA matrix size for GPU stress.")
    return parser.parse_args()


def main() -> int:
    signal.signal(signal.SIGINT, signal.default_int_handler)
    args = parse_args()
    duration = max(1, min(args.duration, 3600))
    workers = max(1, min(args.workers, os.cpu_count() or 1))

    if args.target == "cpu":
        run_cpu(duration, workers)
    elif args.target == "gpu":
        run_gpu(duration, args.gpu_size)
    else:
        process = mp.Process(target=run_cpu, args=(duration, workers))
        process.start()
        try:
            run_gpu(duration, args.gpu_size)
            process.join()
        except KeyboardInterrupt:
            pass
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
