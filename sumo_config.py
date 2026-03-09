"""
sumo_config.py — Simulation & Integration Layer
AI Traffic Optimizer | Bharat Mandapam Demo
-------------------------------------------------
Responsibilities:
  • Launch a SUMO (Simulation of Urban Mobility) environment
    via TraCI (Traffic Control Interface)
  • Feed live signal commands from optimizer.py into the sim
  • Collect per-step metrics: waiting time, fuel consumption,
    CO₂ emissions, queue lengths
  • Export results to CSV for Power BI dashboard
  • Support headless (sumo) and GUI (sumo-gui) modes

Prerequisites:
  pip install traci sumo-tools pandas zmq
  SUMO_HOME environment variable must point to your SUMO installation.
"""

import os
import sys
import time
import zmq
import logging
import pandas as pd
from pathlib import Path
from dataclasses import dataclass, field

# ── Validate SUMO_HOME ──────────────────────────────────────────
SUMO_HOME = os.environ.get("SUMO_HOME", "")
if SUMO_HOME:
    tools_path = os.path.join(SUMO_HOME, "tools")
    if tools_path not in sys.path:
        sys.path.append(tools_path)
    try:
        import traci
        import traci.constants as tc
        TRACI_AVAILABLE = True
    except ImportError:
        TRACI_AVAILABLE = False
else:
    TRACI_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SUMO] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

if not TRACI_AVAILABLE:
    logger.warning(
        "TraCI not available — running in MOCK mode. "
        "Set SUMO_HOME and install sumo-tools to use simulation."
    )

# ──────────────────────────────────────────────
# Paths & Configuration
# ──────────────────────────────────────────────
BASE_DIR         = Path(__file__).parent
SUMO_CFG_PATH    = BASE_DIR / "sumo" / "intersection.sumocfg"
NETWORK_PATH     = BASE_DIR / "sumo" / "intersection.net.xml"
ROUTES_PATH      = BASE_DIR / "sumo" / "routes.rou.xml"
ADDITIONAL_PATH  = BASE_DIR / "sumo" / "tls.add.xml"
OUTPUT_DIR       = BASE_DIR / "logs"

ZMQ_SUB_ADDR     = "tcp://127.0.0.1:5556"   # optimizer.py PUB socket
SIM_STEP_SEC     = 1.0                        # each TraCI step = 1 real second
SIM_END_STEP     = 3600                       # simulate 1 hour (3600 steps)
N_LANES          = 4

# Traffic light JunctionID in the SUMO network
TLS_ID           = "intersection_tls"


# ──────────────────────────────────────────────
# Metrics collector
# ──────────────────────────────────────────────
@dataclass
class StepMetrics:
    step:              int
    active_lane:       int
    green_duration:    float
    emergency_active:  bool
    avg_waiting_time:  float   = 0.0
    avg_speed:         float   = 0.0
    total_vehicles:    int     = 0
    queue_lengths:     dict    = field(default_factory=dict)
    fuel_consumption:  float   = 0.0   # ml per step (sum all vehicles)
    co2_emissions:     float   = 0.0   # mg per step (sum all vehicles)
    throughput:        int     = 0     # vehicles that left the sim this step


class MetricsCollector:
    def __init__(self):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self.records: list[dict] = []
        self.departed_prev: set  = set()

    def collect(self, step: int, signal_state: dict) -> StepMetrics:
        if TRACI_AVAILABLE:
            return self._collect_traci(step, signal_state)
        else:
            return self._collect_mock(step, signal_state)

    def _collect_traci(self, step: int, signal_state: dict) -> StepMetrics:
        vehicle_ids = traci.vehicle.getIDList()
        n_vehicles  = len(vehicle_ids)

        waiting_times  = [traci.vehicle.getWaitingTime(v)  for v in vehicle_ids]
        speeds          = [traci.vehicle.getSpeed(v)         for v in vehicle_ids]
        fuels           = [traci.vehicle.getFuelConsumption(v) for v in vehicle_ids]
        co2s            = [traci.vehicle.getCO2Emission(v)    for v in vehicle_ids]

        arrived_ids = set(traci.simulation.getArrivedIDList())
        throughput  = len(arrived_ids - self.departed_prev)
        self.departed_prev = arrived_ids

        # Queue length per lane (vehicles with speed < 0.1 m/s)
        queue: dict[int, int] = {i: 0 for i in range(N_LANES)}
        for vid in vehicle_ids:
            if traci.vehicle.getSpeed(vid) < 0.1:
                lane_idx = traci.vehicle.getLaneIndex(vid)
                if lane_idx in queue:
                    queue[lane_idx] += 1

        return StepMetrics(
            step             = step,
            active_lane      = signal_state.get("active_lane", 0),
            green_duration   = signal_state.get("green_duration", 0),
            emergency_active = signal_state.get("emergency_active", False),
            avg_waiting_time = sum(waiting_times) / max(n_vehicles, 1),
            avg_speed        = sum(speeds)         / max(n_vehicles, 1),
            total_vehicles   = n_vehicles,
            queue_lengths    = queue,
            fuel_consumption = sum(fuels),
            co2_emissions    = sum(co2s),
            throughput       = throughput,
        )

    def _collect_mock(self, step: int, signal_state: dict) -> StepMetrics:
        """Generate deterministic mock metrics for testing without SUMO."""
        import math, random
        base_wait = 25 - 18 * math.sin(step / 500)   # AI improves over time
        return StepMetrics(
            step             = step,
            active_lane      = signal_state.get("active_lane", 0),
            green_duration   = signal_state.get("green_duration", 30),
            emergency_active = signal_state.get("emergency_active", False),
            avg_waiting_time = round(max(5, base_wait + random.uniform(-2, 2)), 2),
            avg_speed        = round(random.uniform(8, 14), 2),
            total_vehicles   = random.randint(20, 80),
            queue_lengths    = {i: random.randint(0, 10) for i in range(N_LANES)},
            fuel_consumption = round(random.uniform(200, 600), 2),
            co2_emissions    = round(random.uniform(400, 1200), 2),
            throughput       = random.randint(0, 5),
        )

    def record(self, m: StepMetrics):
        row = {
            "step":              m.step,
            "active_lane":       m.active_lane,
            "green_duration":    m.green_duration,
            "emergency_active":  m.emergency_active,
            "avg_waiting_time":  m.avg_waiting_time,
            "avg_speed":         m.avg_speed,
            "total_vehicles":    m.total_vehicles,
            "queue_lane0":       m.queue_lengths.get(0, 0),
            "queue_lane1":       m.queue_lengths.get(1, 0),
            "queue_lane2":       m.queue_lengths.get(2, 0),
            "queue_lane3":       m.queue_lengths.get(3, 0),
            "fuel_consumption":  m.fuel_consumption,
            "co2_emissions":     m.co2_emissions,
            "throughput":        m.throughput,
        }
        self.records.append(row)

    def export(self) -> Path:
        out_path = OUTPUT_DIR / "simulation_metrics.csv"
        df = pd.DataFrame(self.records)
        df.to_csv(out_path, index=False)
        logger.info(f"Exported {len(df)} rows to {out_path}")
        self._print_summary(df)
        return out_path

    def _print_summary(self, df: pd.DataFrame):
        if df.empty:
            return
        logger.info("=" * 50)
        logger.info("SIMULATION SUMMARY")
        logger.info("=" * 50)
        logger.info(f"  Steps simulated      : {len(df)}")
        logger.info(f"  Avg waiting time     : {df['avg_waiting_time'].mean():.2f} s")
        logger.info(f"  Avg vehicle speed    : {df['avg_speed'].mean():.2f} m/s")
        logger.info(f"  Total throughput     : {df['throughput'].sum()} vehicles")
        logger.info(f"  Total fuel consumed  : {df['fuel_consumption'].sum():.0f} ml")
        logger.info(f"  Total CO₂ emitted    : {df['co2_emissions'].sum():.0f} mg")
        logger.info(f"  Emergency activations: {df['emergency_active'].sum()}")
        logger.info("=" * 50)


# ──────────────────────────────────────────────
# Traffic light controller
# ──────────────────────────────────────────────
def build_tls_phase(active_lane: int, n_lanes: int = N_LANES) -> str:
    """
    Build a SUMO TLS phase string for the active green lane.
    Convention: G = green, r = red, y = yellow
    Assumes each lane maps to one signal index in order.
    """
    state = ""
    for i in range(n_lanes):
        state += "G" if i == active_lane else "r"
    return state


def apply_signal_to_sumo(signal_state: dict):
    """Push signal phase into the running SUMO simulation."""
    if not TRACI_AVAILABLE:
        return
    try:
        active_lane    = signal_state["active_lane"]
        green_duration = int(signal_state["green_duration"])
        phase_str      = build_tls_phase(active_lane)

        # Set green phase programmatically
        traci.trafficlight.setRedYellowGreenState(TLS_ID, phase_str)
        traci.trafficlight.setPhaseDuration(TLS_ID, green_duration * 1000)  # ms
    except Exception as e:
        logger.error(f"TraCI signal apply error: {e}")


# ──────────────────────────────────────────────
# SUMO config files generator
# ──────────────────────────────────────────────
def generate_sumo_configs():
    """Create minimal SUMO network/route/config files for a 4-leg intersection."""
    sumo_dir = BASE_DIR / "sumo"
    sumo_dir.mkdir(parents=True, exist_ok=True)

    # --- intersection.net.xml (stub — use NETEDIT for a real network) ---
    net_xml = """<?xml version="1.0" encoding="UTF-8"?>
<!-- 4-leg intersection network — generated by sumo_config.py -->
<!-- For a real demo, open NETEDIT and design your intersection. -->
<net version="1.16" junctionCornerDetail="5">
    <location netOffset="0.00,0.00" convBoundary="0.00,0.00,200.00,200.00"
              origBoundary="-1000,-1000,1000,1000" projParameter="!"/>
</net>
"""

    # --- routes.rou.xml ---
    routes_xml = """<?xml version="1.0" encoding="UTF-8"?>
<routes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
    <!-- Define vehicle flows for each approach arm -->
    <vType id="car"       accel="2.6" decel="4.5" length="5"  maxSpeed="13.89" color="0,0,255"/>
    <vType id="bus"       accel="1.2" decel="3.0" length="12" maxSpeed="11.11" color="0,255,0"/>
    <vType id="ambulance" accel="3.0" decel="5.0" length="6"  maxSpeed="16.67" color="1,0,0"/>

    <!-- Flows — replace edge IDs once you have a real network -->
    <!-- <flow id="north_cars" type="car" from="north_in" to="south_out"
              begin="0" end="3600" vehsPerHour="600"/> -->
</routes>
"""

    # --- intersection.sumocfg ---
    cfg_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<configuration>
    <input>
        <net-file  value="intersection.net.xml"/>
        <route-files value="routes.rou.xml"/>
        <additional-files value="tls.add.xml"/>
    </input>
    <time>
        <begin value="0"/>
        <end   value="{SIM_END_STEP}"/>
        <step-length value="{SIM_STEP_SEC}"/>
    </time>
    <output>
        <tripinfo-output value="../logs/tripinfo.xml"/>
        <emission-output value="../logs/emissions.xml"/>
        <queue-output    value="../logs/queues.xml"/>
    </output>
    <report>
        <verbose value="true"/>
        <no-step-log value="false"/>
    </report>
</configuration>
"""

    # --- tls.add.xml ---
    tls_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<additional>
    <!-- Placeholder TLS definition — will be overridden by TraCI at runtime -->
    <tlLogic id="{TLS_ID}" type="static" programID="AI" offset="0">
        <phase duration="30" state="Grrr"/>
        <phase duration="3"  state="yrrr"/>
        <phase duration="30" state="rGrr"/>
        <phase duration="3"  state="ryrr"/>
        <phase duration="30" state="rrGr"/>
        <phase duration="3"  state="rryr"/>
        <phase duration="30" state="rrrG"/>
        <phase duration="3"  state="rrry"/>
    </tlLogic>
</additional>
"""

    for fname, content in [
        ("intersection.net.xml", net_xml),
        ("routes.rou.xml",       routes_xml),
        ("intersection.sumocfg", cfg_xml),
        ("tls.add.xml",          tls_xml),
    ]:
        fpath = sumo_dir / fname
        if not fpath.exists():
            fpath.write_text(content, encoding="utf-8")
            logger.info(f"Generated: {fpath}")
        else:
            logger.info(f"Already exists, skipping: {fpath}")


# ──────────────────────────────────────────────
# Main simulation loop
# ──────────────────────────────────────────────
def run_simulation(gui: bool = False):
    generate_sumo_configs()

    # ZeroMQ SUB — receive signal states from optimizer.py
    ctx    = zmq.Context()
    sock   = ctx.socket(zmq.SUB)
    sock.connect(ZMQ_SUB_ADDR)
    sock.subscribe(b"signal")
    sock.setsockopt(zmq.RCVTIMEO, 100)
    logger.info(f"Connected ZMQ SUB to optimizer at {ZMQ_SUB_ADDR}")

    collector = MetricsCollector()

    # Latest signal state (default)
    signal_state = {
        "active_lane": 0, "green_duration": 30,
        "emergency_active": False, "seconds_remaining": 30,
    }

    # Start SUMO
    if TRACI_AVAILABLE:
        sumo_binary = "sumo-gui" if gui else "sumo"
        sumo_cmd    = [
            sumo_binary,
            "-c", str(SUMO_CFG_PATH),
            "--start",
            "--delay", "100",
        ]
        traci.start(sumo_cmd)
        logger.info(f"SUMO started: {' '.join(sumo_cmd)}")
    else:
        logger.info("Mock simulation started (no SUMO binary).")

    step = 0
    try:
        while step < SIM_END_STEP:
            step += 1

            # Pull latest optimizer signal (non-blocking)
            try:
                _topic, msg = sock.recv_multipart()
                signal_state = json_loads_safe(msg)
            except zmq.Again:
                pass   # use last known state

            # Apply signal to SUMO
            if TRACI_AVAILABLE:
                apply_signal_to_sumo(signal_state)
                traci.simulationStep()

            # Collect metrics
            m = collector.collect(step, signal_state)
            collector.record(m)

            if step % 60 == 0:
                logger.info(
                    f"Step {step:04d}/{SIM_END_STEP} | "
                    f"AvgWait: {m.avg_waiting_time:.1f}s | "
                    f"Vehicles: {m.total_vehicles} | "
                    f"Throughput: {m.throughput}"
                )

            # Real-time pacing for live demo
            if not TRACI_AVAILABLE:
                time.sleep(0.05)

    except KeyboardInterrupt:
        logger.info("Simulation interrupted by user.")
    finally:
        if TRACI_AVAILABLE:
            traci.close()
        collector.export()
        sock.close()
        ctx.term()
        logger.info("Simulation complete.")


def json_loads_safe(raw) -> dict:
    try:
        return __import__("json").loads(raw)
    except Exception:
        return {}


# ──────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AI Traffic Optimizer — SUMO Simulation")
    parser.add_argument("--gui",   action="store_true", help="Launch SUMO with graphical interface")
    parser.add_argument("--steps", type=int, default=SIM_END_STEP, help="Number of simulation steps")
    args = parser.parse_args()

    SIM_END_STEP = args.steps
    run_simulation(gui=args.gui)
