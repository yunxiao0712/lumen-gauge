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

## Project structure

- `hardware_widget.py`: GTK window, layout, interactions, animations
- `hardware_metrics.py`: local hardware readings and byte formatting
- `stress_test.py`: bounded CPU/GPU load tests for checking widget readings

## Reload

Right-click the widget and choose `重载` to restart the running process after
editing code. You can also send `SIGHUP` to the process.

## Memory Cleanup Permission

Linux cache cleanup requires root permission. If polkit keeps dismissing the
request, install the narrow sudo rule once:

```bash
sudo ./install_memory_cleanup_sudoers.sh
```

It allows only this exact command without a password:
`/usr/bin/tee /proc/sys/vm/drop_caches`.

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
your driver inside a virtual environment. If `uv` is available, use:

```bash
rm -rf .venv
uv venv .venv
uv pip install --python .venv/bin/python torch --index-url https://download.pytorch.org/whl/cu130
.venv/bin/python stress_test.py --target gpu --duration 60
```

Without `uv`, Debian/Ubuntu may need `sudo apt install python3.12-venv` before
creating `.venv` with `python3 -m venv .venv`.

## Requirements

- Linux desktop session
- Python 3
- GTK 3 Python bindings
- Optional: NVIDIA GPU driver tools for GPU metrics
- Optional: sudoers helper or a polkit authentication agent for memory cache cleanup
