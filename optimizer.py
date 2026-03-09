"""
optimizer.py — Intelligence Logic (Brain)
AI Traffic Optimizer | Bharat Mandapam Demo
-------------------------------------------------
Responsibilities:
  • Pull per-lane detection data from detection.py via ZeroMQ
  • Apply the weighted scheduling formula:
       Weight = (Cars × 1) + (Buses × 2) + (Emergency × 100)
  • Maintain a priority queue so the highest-weight lane always
    gets the next green slot
  • Compute dynamic green-light durations (proportional to weight)
  • Push real-time signal state to server.js via ZeroMQ PUB socket
  • Log every decision to a CSV (and optionally SQLite) for Power BI
"""

import time
import zmq
import json
import logging
import heapq
import sqlite3
import pandas as pd
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
ZMQ_PULL_ADDR  = "tcp://127.0.0.1:5555"   # receive from detection.py
ZMQ_PUB_ADDR   = "tcp://127.0.0.1:5556"   # publish signal state to server.js

LOG_CSV_PATH   = Path("logs/signal_log.csv")
LOG_DB_PATH    = Path("logs/traffic.db")

N_LANES        = 4
MIN_GREEN_SEC  = 10     # never give less than 10 s to any lane
MAX_GREEN_SEC  = 90     # cap to avoid indefinite hold
BASE_CYCLE_SEC = 120    # total cycle budget across all lanes
YELLOW_SEC     = 3      # fixed yellow phase
EMERGENCY_HOLD = 60     # seconds to keep an emergency lane green

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [OPTIMIZER] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────
@dataclass
class LaneSnapshot:
    lane_id:      int
    count_cars:   int
    count_buses:  int
    count_trucks: int
    count_motos:  int
    emergency:    bool
    timestamp:    float

    @property
    def weight(self) -> float:
        """
        Core formula:
        W = (Cars×1) + (Motos×0.5) + (Buses×2) + (Trucks×2) + (Emergency×100)
        """
        return (
            self.count_cars   * 1.0
          + self.count_motos  * 0.5
          + self.count_buses  * 2.0
          + self.count_trucks * 2.0
          + (100.0 if self.emergency else 0.0)
        )


@dataclass(order=True)
class QueueEntry:
    """Min-heap entry; negate weight so largest weight comes out first."""
    priority:  float          # -weight  (negated for min-heap → max-heap)
    lane_id:   int = field(compare=False)
    snapshot:  LaneSnapshot = field(compare=False)


@dataclass
class SignalState:
    active_lane:      int
    green_duration:   float
    seconds_remaining: float
    lane_weights:     dict
    emergency_active: bool
    cycle_number:     int
    timestamp:        float = field(default_factory=time.time)


# ──────────────────────────────────────────────
# Logger (CSV + SQLite)
# ──────────────────────────────────────────────
class DecisionLogger:
    COLUMNS = [
        "timestamp", "cycle", "active_lane", "green_duration",
        "w_lane0", "w_lane1", "w_lane2", "w_lane3",
        "emergency_lane", "cars_total", "buses_total",
    ]

    def __init__(self):
        LOG_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._init_csv()
        self._init_db()
        self._buffer: list[dict] = []

    def _init_csv(self):
        if not LOG_CSV_PATH.exists():
            pd.DataFrame(columns=self.COLUMNS).to_csv(LOG_CSV_PATH, index=False)

    def _init_db(self):
        self.conn = sqlite3.connect(str(LOG_DB_PATH), check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS signal_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     REAL,
                cycle         INTEGER,
                active_lane   INTEGER,
                green_duration REAL,
                w_lane0       REAL,
                w_lane1       REAL,
                w_lane2       REAL,
                w_lane3       REAL,
                emergency_lane INTEGER,
                cars_total    INTEGER,
                buses_total   INTEGER
            )
        """)
        self.conn.commit()

    def record(self, state: SignalState, snapshots: dict[int, LaneSnapshot]):
        weights    = state.lane_weights
        emrg_lane  = next((lid for lid, s in snapshots.items() if s.emergency), -1)
        cars_total = sum(s.count_cars  for s in snapshots.values())
        buses_total= sum(s.count_buses for s in snapshots.values())

        row = {
            "timestamp":      state.timestamp,
            "cycle":          state.cycle_number,
            "active_lane":    state.active_lane,
            "green_duration": round(state.green_duration, 2),
            "w_lane0":        round(weights.get(0, 0), 2),
            "w_lane1":        round(weights.get(1, 0), 2),
            "w_lane2":        round(weights.get(2, 0), 2),
            "w_lane3":        round(weights.get(3, 0), 2),
            "emergency_lane": emrg_lane,
            "cars_total":     cars_total,
            "buses_total":    buses_total,
        }
        self._buffer.append(row)

        # Flush every 10 records for performance
        if len(self._buffer) >= 10:
            self._flush()

    def _flush(self):
        if not self._buffer:
            return
        df = pd.DataFrame(self._buffer)
        df.to_csv(LOG_CSV_PATH, mode="a", header=False, index=False)
        self.conn.executemany(
            "INSERT INTO signal_log VALUES (NULL,"
            ":timestamp,:cycle,:active_lane,:green_duration,"
            ":w_lane0,:w_lane1,:w_lane2,:w_lane3,"
            ":emergency_lane,:cars_total,:buses_total)",
            self._buffer,
        )
        self.conn.commit()
        self._buffer.clear()
        logger.debug("Flushed records to CSV and SQLite.")

    def close(self):
        self._flush()
        self.conn.close()


# ──────────────────────────────────────────────
# Weighted scheduler
# ──────────────────────────────────────────────
class TrafficScheduler:
    """
    Priority-queue-based scheduler.
    Algorithm:
      1. Receive fresh LaneSnapshot set from detection.
      2. Build a max-heap keyed on lane weight.
      3. Pop the highest-weight lane → assign green time via ratio:
             green_i = (weight_i / Σ weights) × BASE_CYCLE_SEC
             clamped to [MIN_GREEN_SEC, MAX_GREEN_SEC]
      4. Emergency lane always jumps to the front with EMERGENCY_HOLD seconds.
    """

    def __init__(self):
        self.heap:      list[QueueEntry]          = []
        self.snapshots: dict[int, LaneSnapshot]   = {}
        self.cycle:     int                        = 0
        self.logger:    DecisionLogger             = DecisionLogger()
        self._emrg_cooldown: Optional[float]       = None

    def update_from_payload(self, payload: dict):
        """Ingest the latest JSON payload from detection.py."""
        self.snapshots.clear()
        for lane_data in payload["lanes"]:
            snap = LaneSnapshot(
                lane_id      = lane_data["lane_id"],
                count_cars   = lane_data["count_cars"],
                count_buses  = lane_data["count_buses"],
                count_trucks = lane_data.get("count_trucks", 0),
                count_motos  = lane_data.get("count_motos",  0),
                emergency    = lane_data["emergency"],
                timestamp    = lane_data["timestamp"],
            )
            self.snapshots[snap.lane_id] = snap

    def build_queue(self):
        """Rebuild the max-heap from current snapshots."""
        self.heap = []
        for snap in self.snapshots.values():
            entry = QueueEntry(priority=-snap.weight, lane_id=snap.lane_id, snapshot=snap)
            heapq.heappush(self.heap, entry)

    def next_green(self) -> SignalState:
        """
        Return the SignalState for the next green phase.
        Emergency lanes pre-empt queue ordering.
        """
        self.build_queue()
        self.cycle += 1

        # Check for emergency pre-emption
        emergency_snap = next(
            (s for s in self.snapshots.values() if s.emergency), None
        )
        if emergency_snap:
            green_duration = EMERGENCY_HOLD
            active_lane    = emergency_snap.lane_id
            logger.warning(
                f"EMERGENCY PRE-EMPTION: Lane {active_lane} — "
                f"holding green for {green_duration}s"
            )
        else:
            if not self.heap:
                # Fallback: round-robin
                active_lane    = self.cycle % N_LANES
                green_duration = BASE_CYCLE_SEC // N_LANES
            else:
                top        = heapq.heappop(self.heap)
                active_lane= top.lane_id
                total_w    = max(sum(s.weight for s in self.snapshots.values()), 1)
                ratio      = top.snapshot.weight / total_w
                green_duration = max(
                    MIN_GREEN_SEC,
                    min(MAX_GREEN_SEC, ratio * BASE_CYCLE_SEC)
                )

        weights = {lid: round(s.weight, 2) for lid, s in self.snapshots.items()}

        state = SignalState(
            active_lane       = active_lane,
            green_duration    = round(green_duration, 1),
            seconds_remaining = round(green_duration, 1),
            lane_weights      = weights,
            emergency_active  = emergency_snap is not None,
            cycle_number      = self.cycle,
        )

        logger.info(
            f"Cycle {self.cycle:04d} | Green → Lane {active_lane} "
            f"for {green_duration:.1f}s | Weights: {weights}"
        )
        self.logger.record(state, self.snapshots)
        return state

    def tick_countdown(self, state: SignalState, elapsed: float) -> SignalState:
        """Decrement seconds_remaining by elapsed seconds."""
        state.seconds_remaining = max(0.0, round(state.seconds_remaining - elapsed, 1))
        return state


# ──────────────────────────────────────────────
# Main optimizer loop
# ──────────────────────────────────────────────
def run_optimizer():
    ctx  = zmq.Context()

    # PULL from detection.py
    pull = ctx.socket(zmq.PULL)
    pull.connect(ZMQ_PULL_ADDR)
    pull.setsockopt(zmq.RCVTIMEO, 500)   # 500 ms timeout

    # PUB to server.js
    pub  = ctx.socket(zmq.PUB)
    pub.bind(ZMQ_PUB_ADDR)
    logger.info(f"ZeroMQ PUB socket bound to {ZMQ_PUB_ADDR}")

    scheduler = TrafficScheduler()
    current_state: Optional[SignalState] = None
    phase_start: float = time.time()

    logger.info("Optimizer running. Waiting for detection data…")
    try:
        while True:
            # Try to receive a fresh detection frame
            try:
                payload = pull.recv_json()
                scheduler.update_from_payload(payload)
            except zmq.Again:
                pass   # no new frame; continue with stale data

            now = time.time()

            # Decide next green phase when:
            #   • we have no current state, OR
            #   • the current green phase has expired
            if current_state is None or current_state.seconds_remaining <= 0:
                if scheduler.snapshots:
                    current_state = scheduler.next_green()
                    phase_start   = now
                else:
                    # No data yet — default equal split
                    logger.debug("No snapshot data yet; waiting…")
                    time.sleep(0.5)
                    continue
            else:
                # Update countdown
                elapsed = now - phase_start
                current_state = scheduler.tick_countdown(current_state, elapsed)
                phase_start   = now

            # Publish state to Node.js
            pub.send_string("signal", zmq.SNDMORE)
            pub.send_json({
                "active_lane":       current_state.active_lane,
                "green_duration":    current_state.green_duration,
                "seconds_remaining": current_state.seconds_remaining,
                "lane_weights":      current_state.lane_weights,
                "emergency_active":  current_state.emergency_active,
                "cycle_number":      current_state.cycle_number,
                "timestamp":         current_state.timestamp,
                # Include latest densities
                "lane_densities": {
                    lid: (snap.count_cars + snap.count_buses + snap.count_trucks + snap.count_motos)
                    for lid, snap in scheduler.snapshots.items()
                },
            })

            time.sleep(0.2)   # 5 Hz publish rate

    except KeyboardInterrupt:
        logger.info("Optimizer interrupted.")
    finally:
        scheduler.logger.close()
        pull.close()
        pub.close()
        ctx.term()
        logger.info("Optimizer shut down.")


# ──────────────────────────────────────────────
if __name__ == "__main__":
    run_optimizer()
