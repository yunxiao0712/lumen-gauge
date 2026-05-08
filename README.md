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

## Requirements

- Linux desktop session
- Python 3
- GTK 3 Python bindings
- Optional: NVIDIA GPU driver tools for GPU metrics
