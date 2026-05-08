# Lumen Gauge

Lumen Gauge is a lightweight GTK desktop widget for Linux that shows live local hardware usage in a compact glass-style panel.

## Features

- CPU, GPU, memory, disk, home partition, and battery rings
- Per-device temperature bars
- Upload and download network charts
- Edge snap mode with a compact network strip
- Single-instance guard
- Left-button drag, right-click exit

## Run

```bash
./run.sh
```

The widget reads local Linux system data from `/proc`, `/sys`, and `nvidia-smi` when available.

## Hardware load test

Use the bounded test script while the widget is open:

```bash
python3 stress_test.py --target cpu --duration 60
python3 stress_test.py --target gpu --duration 60
python3 stress_test.py --target both --duration 60
```

CPU load uses local Python worker processes. GPU load uses PyTorch CUDA when available.
Press `Ctrl+C` to stop early.

If GPU testing reports that PyTorch is missing, install a CUDA wheel that matches
your driver. For a driver that reports CUDA 13.0:

```bash
python3 -m pip install torch --index-url https://download.pytorch.org/whl/cu130
```

## Requirements

- Linux desktop session
- Python 3
- GTK 3 Python bindings
- Optional: NVIDIA GPU driver tools for GPU metrics
