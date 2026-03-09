"""
detection.py — Perception Engine
AI Traffic Optimizer | Bharat Mandapam Demo
-------------------------------------------------
Responsibilities:
  • Read video frames via OpenCV (webcam or RTSP feed)
  • Run YOLOv8 inference to detect vehicles + ambulances
  • Count bounding-box centres inside a configurable ROI
  • Broadcast structured results to optimizer.py via a
    shared multiprocessing Queue / ZeroMQ socket
  • Raise an immediate EMERGENCY flag when an ambulance
    is detected in any lane
"""

import cv2
import time
import zmq
import json
import logging
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

# Optional: ultralytics YOLOv8 — pip install ultralytics
try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False
    logging.warning("ultralytics not found running in MOCK mode.")

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
ZMQ_PUSH_ADDR  = "tcp://127.0.0.1:5555"   # push results to optimizer
MODEL_PATH     = "yolov8n.pt"              # nano model; swap for yolov8m.pt for accuracy
CONF_THRESHOLD = 0.40
IOU_THRESHOLD  = 0.45
TARGET_FPS     = 10                        # process at most 10 fps to save CPU
FRAME_WIDTH    = 1280
FRAME_HEIGHT   = 720

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DETECTION] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# COCO class IDs relevant to traffic
# ──────────────────────────────────────────────
COCO_LABELS = {
    2:  "car",
    3:  "motorcycle",
    5:  "bus",
    7:  "truck",
    # YOLOv8 fine-tuned models include class 80 onwards for custom labels;
    # adjust the integer if using a custom-trained ambulance class.
}
AMBULANCE_CLASS_ID = 999   # custom fine-tuned class; set to correct ID

# Vehicle weights used by the optimizer formula
VEHICLE_WEIGHT_MAP = {
    "car":        1,
    "motorcycle": 0.5,
    "bus":        2,
    "truck":      2,
    "ambulance":  100,   # triggers emergency pre-emption
}


# ──────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────
@dataclass
class LaneState:
    lane_id:        int
    count_cars:     int   = 0
    count_buses:    int   = 0
    count_trucks:   int   = 0
    count_motos:    int   = 0
    emergency:      bool  = False
    raw_detections: list  = field(default_factory=list)
    timestamp:      float = field(default_factory=time.time)

    @property
    def weighted_score(self) -> float:
        score = (
            self.count_cars   * VEHICLE_WEIGHT_MAP["car"]
          + self.count_buses  * VEHICLE_WEIGHT_MAP["bus"]
          + self.count_trucks * VEHICLE_WEIGHT_MAP["truck"]
          + self.count_motos  * VEHICLE_WEIGHT_MAP["motorcycle"]
        )
        if self.emergency:
            score += VEHICLE_WEIGHT_MAP["ambulance"]
        return score


# ──────────────────────────────────────────────
# Region of Interest helpers
# ──────────────────────────────────────────────
def build_rois(frame_w: int, frame_h: int, n_lanes: int = 4) -> list[dict]:
    """
    Divide the frame into n_lanes vertical strips.
    Each strip acts as an ROI for one lane.
    Returns a list of dicts: {lane_id, x1, y1, x2, y2}
    """
    strip_w = frame_w // n_lanes
    # Use the lower 60 % of the frame (where road traffic concentrates)
    y1 = int(frame_h * 0.40)
    y2 = frame_h
    rois = []
    for i in range(n_lanes):
        rois.append({
            "lane_id": i,
            "x1": i * strip_w,
            "y1": y1,
            "x2": (i + 1) * strip_w,
            "y2": y2,
        })
    return rois


def point_in_roi(cx: float, cy: float, roi: dict) -> bool:
    return roi["x1"] <= cx <= roi["x2"] and roi["y1"] <= cy <= roi["y2"]


# ──────────────────────────────────────────────
# Mock detector (used when ultralytics absent)
# ──────────────────────────────────────────────
class MockDetector:
    """Returns random detections so the pipeline can be tested end-to-end."""
    CLASSES = ["car", "car", "car", "bus", "motorcycle", "truck", "ambulance"]

    def detect(self, frame: np.ndarray) -> list[dict]:
        import random
        detections = []
        h, w = frame.shape[:2]
        for _ in range(random.randint(2, 8)):
            cls = random.choice(self.CLASSES)
            cx  = random.randint(50, w - 50)
            cy  = random.randint(int(h * 0.4), h - 20)
            detections.append({"class": cls, "cx": cx, "cy": cy, "conf": round(random.uniform(0.5, 0.99), 2)})
        return detections


# ──────────────────────────────────────────────
# YOLO detector wrapper
# ──────────────────────────────────────────────
class YOLODetector:
    def __init__(self, model_path: str):
        logger.info(f"Loading YOLO model from: {model_path}")
        self.model = YOLO(model_path)
        logger.info("Model loaded successfully.")

    def detect(self, frame: np.ndarray) -> list[dict]:
        results = self.model(
            frame,
            conf=CONF_THRESHOLD,
            iou=IOU_THRESHOLD,
            verbose=False,
        )[0]
        detections = []
        for box in results.boxes:
            cls_id  = int(box.cls[0])
            conf    = float(box.conf[0])
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2
            label = COCO_LABELS.get(cls_id, None)
            if cls_id == AMBULANCE_CLASS_ID:
                label = "ambulance"
            if label:
                detections.append({"class": label, "cx": cx, "cy": cy, "conf": conf})
        return detections


# ──────────────────────────────────────────────
# Enhanced Frame annotator for Demo (with lane counters & congestion bars)
# ──────────────────────────────────────────────
def annotate_frame(frame: np.ndarray, detections: list[dict],
                   rois: list[dict], lane_states: list[LaneState]) -> np.ndarray:
    overlay = frame.copy()
    h, w = frame.shape[:2]

    # Draw dashboard background at top
    dashboard_height = 120
    cv2.rectangle(overlay, (0, 0), (w, dashboard_height), (40, 40, 40), -1)
    cv2.rectangle(overlay, (0, 0), (w, dashboard_height), (255, 255, 255), 2)

    # Title
    cv2.putText(overlay, "AI TRAFFIC OPTIMIZER - BHARAT MANDAPAM DEMO", 
                (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
    
    # ROI colors for lanes
    roi_colors = [(0, 255, 0), (255, 165, 0), (0, 165, 255), (255, 0, 255)]
    
    # Track max weight for congestion bar scaling
    max_weight = max([s.weighted_score for s in lane_states] + [1.0])
    
    # Draw ROIs with enhanced labels
    for roi in rois:
        color = roi_colors[roi["lane_id"] % len(roi_colors)]
        state = next((s for s in lane_states if s.lane_id == roi["lane_id"]), None)
        
        # Emergency detection - make lane flash red
        if state and state.emergency:
            cv2.rectangle(overlay, (roi["x1"], roi["y1"]), (roi["x2"], roi["y2"]), (0, 0, 255), 6)
            # Emergency banner
            cv2.rectangle(overlay, (roi["x1"], roi["y1"]-40), (roi["x2"], roi["y1"]), (0, 0, 255), -1)
            cv2.putText(overlay, "!!! EMERGENCY !!!", (roi["x1"] + 10, roi["y1"]-15), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        else:
            cv2.rectangle(overlay, (roi["x1"], roi["y1"]), (roi["x2"], roi["y2"]), color, 3)
        
        # Enhanced lane labels
        if state:
            total_vehicles = state.count_cars + state.count_buses + state.count_trucks + state.count_motos
            
            # Vehicle count breakdown
            count_text = f"LANE {roi['lane_id']}: {total_vehicles} vehicles"
            cv2.putText(overlay, count_text, (roi["x1"] + 5, roi["y1"] + 25), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            
            # Detailed breakdown
            breakdown = f"Cars:{state.count_cars} Bus:{state.count_buses} Moto:{state.count_motos}"
            cv2.putText(overlay, breakdown, (roi["x1"] + 5, roi["y1"] + 45), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
            
            # Weight score
            weight_text = f"Priority Weight: {state.weighted_score:.1f}"
            cv2.putText(overlay, weight_text, (roi["x1"] + 5, roi["y1"] + 65), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)

    # Draw enhanced lane counters in dashboard
    lane_panel_width = w // 4
    for i, state in enumerate(lane_states):
        x_start = i * lane_panel_width + 10
        y_pos = 45
        
        # Lane header
        lane_color = roi_colors[i % len(roi_colors)]
        cv2.putText(overlay, f"LANE {i}", (x_start, y_pos), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, lane_color, 2)
        
        # Vehicle counts with icons
        total = state.count_cars + state.count_buses + state.count_trucks + state.count_motos
        cv2.putText(overlay, f"Vehicles: {total}", (x_start, y_pos + 20), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
        
        # Congestion bar
        bar_width = lane_panel_width - 40
        bar_height = 15
        bar_x = x_start
        bar_y = y_pos + 30
        
        # Background bar
        cv2.rectangle(overlay, (bar_x, bar_y), (bar_x + bar_width, bar_y + bar_height), 
                      (60, 60, 60), -1)
        cv2.rectangle(overlay, (bar_x, bar_y), (bar_x + bar_width, bar_y + bar_height), 
                      (255, 255, 255), 1)
        
        # Congestion fill
        if max_weight > 0:
            fill_width = int((state.weighted_score / max_weight) * bar_width)
            # Color based on congestion level
            if state.emergency:
                bar_color = (0, 0, 255)  # Red for emergency
            elif state.weighted_score > max_weight * 0.7:
                bar_color = (0, 165, 255)  # Orange for high traffic
            elif state.weighted_score > max_weight * 0.3:
                bar_color = (0, 255, 255)  # Yellow for medium traffic
            else:
                bar_color = (0, 255, 0)    # Green for low traffic
                
            cv2.rectangle(overlay, (bar_x, bar_y), (bar_x + fill_width, bar_y + bar_height), 
                          bar_color, -1)
        
        # Weight score
        cv2.putText(overlay, f"W:{state.weighted_score:.1f}", (x_start, y_pos + 60), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)

    # Draw detection centres with enhanced labels
    for det in detections:
        dot_color = (0, 0, 255) if det["class"] == "ambulance" else (0, 255, 255)
        circle_size = 10 if det["class"] == "ambulance" else 6
        
        cv2.circle(overlay, (int(det["cx"]), int(det["cy"])), circle_size, dot_color, -1)
        cv2.circle(overlay, (int(det["cx"]), int(det["cy"])), circle_size + 2, (255, 255, 255), 2)
        
        # Enhanced labels
        label_text = f"{det['class'].upper()}"
        if det["class"] == "ambulance":
            label_text = "[!] " + label_text
            
        cv2.putText(overlay, label_text, (int(det["cx"]) + 12, int(det["cy"]) - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, dot_color, 2)

    # Real-time timestamp
    cv2.putText(overlay, f"Live Feed: {time.strftime('%H:%M:%S')}", (w-200, 25), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
    
    # System status
    emergency_active = any(s.emergency for s in lane_states)
    status_text = "EMERGENCY MODE" if emergency_active else "NORMAL OPERATION"
    status_color = (0, 0, 255) if emergency_active else (0, 255, 0)
    cv2.putText(overlay, f"Status: {status_text}", (w-200, 45), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, status_color, 1)

    cv2.addWeighted(overlay, 0.9, frame, 0.1, 0, frame)
    return frame


# ──────────────────────────────────────────────
# Main detection loop
# ──────────────────────────────────────────────
def run_detection(video_source=0, n_lanes: int = 4, display: bool = True):
    """
    video_source: integer (webcam index), file path, or RTSP URL.
    Publishes LaneState JSON over ZeroMQ PUSH socket.
    """
    # ZeroMQ publisher
    ctx    = zmq.Context()
    socket = ctx.socket(zmq.PUSH)
    socket.bind(ZMQ_PUSH_ADDR)
    logger.info(f"ZeroMQ PUSH socket bound to {ZMQ_PUSH_ADDR}")

    # Detector
    detector = YOLODetector(MODEL_PATH) if YOLO_AVAILABLE and Path(MODEL_PATH).exists() else MockDetector()
    logger.info(f"Using detector: {detector.__class__.__name__}")

    # Video capture
    cap = cv2.VideoCapture(video_source)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    if not cap.isOpened():
        logger.error(f"Cannot open video source: {video_source}")
        return

    frame_interval = 1.0 / TARGET_FPS
    last_time      = time.time()

    logger.info("Detection loop started. Press 'q' to quit.")
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                logger.warning("Frame read failed — end of stream or camera error.")
                break

            now = time.time()
            if (now - last_time) < frame_interval:
                continue
            last_time = now

            rois         = build_rois(frame.shape[1], frame.shape[0], n_lanes)
            detections   = detector.detect(frame)

            # Aggregate counts per lane
            lane_states: list[LaneState] = []
            for roi in rois:
                state = LaneState(lane_id=roi["lane_id"])
                for det in detections:
                    if point_in_roi(det["cx"], det["cy"], roi):
                        cls = det["class"]
                        if cls == "car":          state.count_cars  += 1
                        elif cls == "bus":        state.count_buses += 1
                        elif cls == "truck":      state.count_trucks+= 1
                        elif cls == "motorcycle": state.count_motos += 1
                        elif cls == "ambulance":  state.emergency    = True
                        state.raw_detections.append(det)
                lane_states.append(state)

            # Publish to optimizer
            payload = {
                "lanes":     [asdict(s) for s in lane_states],
                "timestamp": time.time(),
            }
            socket.send_json(payload, zmq.NOBLOCK)

            # Emergency log
            emergency_lanes = [s.lane_id for s in lane_states if s.emergency]
            if emergency_lanes:
                logger.warning(f"EMERGENCY detected in lanes: {emergency_lanes}")

            # Optional display
            if display:
                annotated = annotate_frame(frame.copy(), detections, rois, lane_states)
                cv2.imshow("AI Traffic Optimizer — Perception", annotated)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    logger.info("Quit key pressed.")
                    break

    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        cap.release()
        cv2.destroyAllWindows()
        socket.close()
        ctx.term()
        logger.info("Detection engine shut down.")


# ──────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AI Traffic Optimizer — Perception Engine")
    parser.add_argument("--source",   default=0,    help="Video source: 0=webcam, path, or RTSP URL")
    parser.add_argument("--lanes",    type=int, default=4, help="Number of lanes to monitor")
    parser.add_argument("--no-display", action="store_true", help="Run headless (no cv2 window)")
    args = parser.parse_args()

    run_detection(
        video_source=args.source,
        n_lanes=args.lanes,
        display=not args.no_display,
    )
