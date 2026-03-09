#!/usr/bin/env python3
"""
demo_setup.py - Quick Demo Environment Setup
AI Traffic Optimizer | Bharat Mandapam Demo
-------------------------------------------------
This script helps you verify and set up the demo environment quickly.
Run this before starting your demo to ensure everything is ready.
"""

import sys
import subprocess
import os
from pathlib import Path
import json

def check_python_version():
    """Verify Python 3.10+ is installed"""
    version = sys.version_info
    if version.major == 3 and version.minor >= 10:
        print(f"✅ Python {version.major}.{version.minor}.{version.micro} - OK")
        return True
    else:
        print(f"❌ Python version {version.major}.{version.minor} is too old. Need 3.10+")
        return False

def check_video_file():
    """Check if demo video exists"""
    video_files = list(Path(".").glob("*.mp4")) + list(Path(".").glob("*.avi"))
    if video_files:
        print(f"✅ Found video file: {video_files[0]}")
        return str(video_files[0])
    else:
        print("⚠️  No video file found. Please download a traffic video and place it in this folder.")
        print("   Example: traffic_video.mp4")
        return None

def check_requirements():
    """Check if Python requirements are installed"""
    try:
        import cv2
        print("✅ OpenCV installed")
    except ImportError:
        print("❌ OpenCV not installed. Run: pip install opencv-python")
        return False
    
    try:
        import zmq
        print("✅ ZeroMQ installed")
    except ImportError:
        print("❌ ZeroMQ not installed. Run: pip install pyzmq")
        return False
    
    try:
        from ultralytics import YOLO
        print("✅ Ultralytics YOLO installed")
    except ImportError:
        print("❌ Ultralytics not installed. Run: pip install ultralytics")
        return False
    
    try:
        import pandas
        print("✅ Pandas installed")
    except ImportError:
        print("❌ Pandas not installed. Run: pip install pandas")
        return False
    
    return True

def check_node_modules():
    """Check if Node.js dependencies are installed"""
    if Path("node_modules").exists() and Path("package.json").exists():
        print("✅ Node.js dependencies installed")
        return True
    else:
        print("❌ Node.js dependencies not installed. Run: npm install")
        return False

def check_env_file():
    """Check if .env file exists"""
    if Path(".env").exists():
        print("✅ Environment file (.env) exists")
        return True
    else:
        print("⚠️  .env file not found. Creating from template...")
        env_content = """PORT=4000
ZMQ_SUB_ADDR=tcp://127.0.0.1:5556
JWT_SECRET=supersecuresecret123demo
JWT_EXPIRES_IN=8h
N_LANES=4
CORS_ORIGIN=http://localhost:3000
"""
        Path(".env").write_text(env_content)
        print("✅ Created .env file with demo settings")
        return True

def download_yolo_model():
    """Pre-download YOLO model to avoid demo delays"""
    try:
        print("📥 Downloading YOLO model (this may take a moment)...")
        from ultralytics import YOLO
        YOLO('yolov8n.pt')  # This downloads the model
        print("✅ YOLO model ready")
        return True
    except Exception as e:
        print(f"❌ Failed to download YOLO model: {e}")
        return False

def create_demo_script():
    """Create a batch script to start all processes"""
    if os.name == 'nt':  # Windows
        script_content = """@echo off
echo Starting AI Traffic Optimizer Demo...
echo.
echo Opening 4 terminal windows...
echo.

echo Terminal 1: Detection Engine
start cmd /k "python detection.py --source traffic_video.mp4"

timeout /t 3

echo Terminal 2: Optimizer Brain  
start cmd /k "python optimizer.py"

timeout /t 3

echo Terminal 3: API Server
start cmd /k "npm start"

timeout /t 3

echo Terminal 4: SUMO Simulation
start cmd /k "python sumo_config.py --gui"

echo.
echo Demo started! Arrange windows for presentation.
echo Press any key to exit this window...
pause
"""
        Path("start_demo.bat").write_text(script_content)
        print("✅ Created start_demo.bat - you can use this to launch the entire demo")
    else:  # Linux/Mac
        script_content = """#!/bin/bash
echo "Starting AI Traffic Optimizer Demo..."
echo

echo "Terminal 1: Detection Engine"
gnome-terminal -- bash -c "python detection.py --source traffic_video.mp4; exec bash"

sleep 3

echo "Terminal 2: Optimizer Brain"
gnome-terminal -- bash -c "python optimizer.py; exec bash"

sleep 3

echo "Terminal 3: API Server"
gnome-terminal -- bash -c "npm start; exec bash"

sleep 3

echo "Terminal 4: SUMO Simulation"
gnome-terminal -- bash -c "python sumo_config.py --gui; exec bash"

echo "Demo started! Arrange windows for presentation."
"""
        Path("start_demo.sh").write_text(script_content)
        os.chmod("start_demo.sh", 0o755)
        print("✅ Created start_demo.sh - you can use this to launch the entire demo")

def main():
    """Main setup verification"""
    print("=" * 60)
    print("AI TRAFFIC OPTIMIZER - DEMO SETUP VERIFICATION")
    print("=" * 60)
    print()
    
    all_good = True
    
    # Check Python version
    if not check_python_version():
        all_good = False
    
    # Check video file
    video_file = check_video_file()
    if not video_file:
        all_good = False
    
    # Check Python requirements
    if not check_requirements():
        all_good = False
    
    # Check Node modules
    if not check_node_modules():
        all_good = False
    
    # Check/create .env file
    check_env_file()
    
    # Download YOLO model
    if not download_yolo_model():
        all_good = False
    
    # Create demo startup script
    create_demo_script()
    
    print()
    print("=" * 60)
    if all_good:
        print("🎉 DEMO SETUP COMPLETE! You're ready to go!")
        print()
        print("Quick Start:")
        if os.name == 'nt':
            print("   Double-click: start_demo.bat")
        else:
            print("   Run: ./start_demo.sh")
        print()
        print("Manual Start Order:")
        print("   1️⃣  python detection.py --source traffic_video.mp4")
        print("   2️⃣  python optimizer.py")
        print("   3️⃣  npm start")
        print("   4️⃣  python sumo_config.py --gui")
        print()
        print("Test API: http://localhost:4000/api/health")
    else:
        print("❌ Setup incomplete. Please fix the issues above.")
    print("=" * 60)

if __name__ == "__main__":
    main()