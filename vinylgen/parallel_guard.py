"""Process-wide serialisation of entry into parallel-Numba kernels.

One @njit(parallel=True) call already saturates every core, so concurrent
callers (the CLI flattens independent images on a thread pool) gain nothing from
overlapping. A single shared lock serialises entry into every parallel kernel
across the colour, segmentation, and ridge modules alike, so numba's workqueue
threading layer -- which rejects concurrent parallel entry when TBB and OpenMP
are both unavailable -- never sees two parallel regions at once. A per-module
lock would leave two different modules' kernels free to enter concurrently, so
the guard is deliberately one object shared by every kernel.
"""
from __future__ import annotations

import threading

KERNEL_LOCK = threading.Lock()
