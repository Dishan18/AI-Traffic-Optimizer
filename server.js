/**
 * server.js — Command Center API
 * AI Traffic Optimizer | Bharat Mandapam Demo
 * --------------------------------------------------
 * Responsibilities:
 *   • Subscribe to optimizer.py via ZeroMQ (SUB socket)
 *   • Push live signal state to React frontend via Socket.io
 *   • Expose REST endpoints for manual override & status
 *   • JWT authentication for protected control routes
 *   • Rate-limiting & helmet for basic hardening
 */

"use strict";

const express = require("express");
const http = require("http");
const { Server } = require("socket.io");
const zmq = require("zeromq");
const jwt = require("jsonwebtoken");
const bcrypt = require("bcryptjs");
const helmet = require("helmet");
const cors = require("cors");
const rateLimit = require("express-rate-limit");
const morgan = require("morgan");
const path = require("path");
require("dotenv").config();

// ──────────────────────────────────────────────
// Config
// ──────────────────────────────────────────────
const PORT = process.env.PORT || 4000;
const ZMQ_SUB_ADDR = process.env.ZMQ_SUB_ADDR || "tcp://127.0.0.1:5556";
const JWT_SECRET = process.env.JWT_SECRET || "change_this_in_production_please";
const JWT_EXPIRES_IN = process.env.JWT_EXPIRES_IN || "8h";
const N_LANES = parseInt(process.env.N_LANES || "4", 10);
const CORS_ORIGIN = process.env.CORS_ORIGIN || "http://localhost:3000";

// ──────────────────────────────────────────────
// In-memory state (replace with Redis in prod)
// ──────────────────────────────────────────────
let latestSignalState = {
  active_lane: 0,
  green_duration: 30,
  seconds_remaining: 30,
  lane_weights: Object.fromEntries([...Array(N_LANES)].map((_, i) => [i, 0])),
  emergency_active: false,
  cycle_number: 0,
  lane_densities: Object.fromEntries([...Array(N_LANES)].map((_, i) => [i, 0])),
  timestamp: Date.now() / 1000,
};

// Manual override state: { active: bool, lane: int, duration: int, expiresAt: number }
let manualOverride = {
  active: false,
  lane: null,
  duration: null,
  expiresAt: null,
};

// Mock user store (in production: MongoDB with hashed passwords)
const USERS = [
  {
    id: 1,
    username: "admin",
    // bcrypt hash of "Admin@1234" — regenerate in production
    password: "$2a$10$WFrqMkvsWiZKwt4qVh4CKuDnW1dgDKiqI6gEq.zDIhqlWciO3XKo2",
    role: "admin",
  },
  {
    id: 2,
    username: "officer",
    password: "$2a$10$WFrqMkvsWiZKwt4qVh4CKuDnW1dgDKiqI6gEq.zDIhqlWciO3XKo2",
    role: "officer",
  },
];

// ──────────────────────────────────────────────
// Express & Socket.io bootstrap
// ──────────────────────────────────────────────
const app = express();
const server = http.createServer(app);
const io = new Server(server, {
  cors: { origin: "*", methods: ["GET", "POST"] },
});

app.use(helmet({ contentSecurityPolicy: false }));
app.use(cors({ origin: "*" }));
app.use(morgan("combined"));
app.use(express.json());

// Global rate limiter (100 req / 15 min per IP)
const globalLimiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 100,
  standardHeaders: true,
  legacyHeaders: false,
});
app.use(globalLimiter);

// Strict limiter for auth routes (5 attempts / 15 min)
const authLimiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 5,
  message: { error: "Too many login attempts. Try again later." },
});

// ──────────────────────────────────────────────
// JWT middleware
// ──────────────────────────────────────────────
function authenticateJWT(req, res, next) {
  const authHeader = req.headers["authorization"];
  if (!authHeader?.startsWith("Bearer ")) {
    return res
      .status(401)
      .json({ error: "Missing or malformed Authorization header." });
  }
  const token = authHeader.split(" ")[1];
  jwt.verify(token, JWT_SECRET, (err, user) => {
    if (err)
      return res.status(403).json({ error: "Invalid or expired token." });
    req.user = user;
    next();
  });
}

function requireRole(...roles) {
  return (req, res, next) => {
    if (!roles.includes(req.user?.role)) {
      return res.status(403).json({ error: "Insufficient permissions." });
    }
    next();
  };
}

// ──────────────────────────────────────────────
// REST Routes — Public
// ──────────────────────────────────────────────

/** Serve Command Center Dashboard */
app.get("/", (_req, res) => {
  res.sendFile(path.join(__dirname, "dashboard.html"));
});

/** Health check */
app.get("/api/health", (_req, res) => {
  res.json({
    status: "ok",
    uptime: process.uptime(),
    timestamp: new Date().toISOString(),
  });
});

/** Login — returns JWT */
app.post("/api/auth/login", authLimiter, async (req, res) => {
  const { username, password } = req.body;
  if (!username || !password) {
    return res
      .status(400)
      .json({ error: "username and password are required." });
  }
  const user = USERS.find((u) => u.username === username);
  if (!user) {
    return res.status(401).json({ error: "Invalid credentials." });
  }
  const match = await bcrypt.compare(password, user.password);
  if (!match) {
    return res.status(401).json({ error: "Invalid credentials." });
  }
  const token = jwt.sign(
    { id: user.id, username: user.username, role: user.role },
    JWT_SECRET,
    { expiresIn: JWT_EXPIRES_IN },
  );
  res.json({ token, role: user.role, expiresIn: JWT_EXPIRES_IN });
});

/** Live signal state — read-only public endpoint */
app.get("/api/signal/state", (_req, res) => {
  res.json({ ...latestSignalState, override: manualOverride });
});

// ──────────────────────────────────────────────
// REST Routes — Protected (JWT required)
// ──────────────────────────────────────────────

/**
 * Manual override — traffic official forces a specific lane green.
 * Body: { lane: 0-3, duration: 10-120 }
 */
app.post(
  "/api/signal/override",
  authenticateJWT,
  requireRole("admin", "officer"),
  (req, res) => {
    const { lane, duration } = req.body;
    if (lane < 0 || lane >= N_LANES || !Number.isInteger(lane)) {
      return res
        .status(400)
        .json({
          error: `lane must be an integer between 0 and ${N_LANES - 1}.`,
        });
    }
    if (duration < 10 || duration > 120 || !Number.isInteger(duration)) {
      return res
        .status(400)
        .json({ error: "duration must be between 10 and 120 seconds." });
    }

    manualOverride = {
      active: true,
      lane: lane,
      duration: duration,
      expiresAt: Date.now() + duration * 1000,
      issuedBy: req.user.username,
    };

    // Update active state immediately so the ticker and UI synchronize
    latestSignalState.active_lane = lane;
    latestSignalState.seconds_remaining = duration;

    // Broadcast override and full state update immediately
    io.emit("signal:override", manualOverride);
    io.emit("signal:update", {
      ...latestSignalState,
      override: manualOverride,
    });

    console.log(
      `[OVERRIDE] ${req.user.username} → Lane ${lane} for ${duration}s`,
    );
    res.json({ message: "Override applied.", override: manualOverride });
  },
);

/** Cancel an active override */
app.delete(
  "/api/signal/override",
  authenticateJWT,
  requireRole("admin", "officer"),
  (req, res) => {
    manualOverride = {
      active: false,
      lane: null,
      duration: null,
      expiresAt: null,
    };
    io.emit("signal:override_cancelled", { issuedBy: req.user.username });
    res.json({ message: "Override cancelled." });
  },
);

/** Get real-time lane statistics */
app.get("/api/lanes/stats", authenticateJWT, (_req, res) => {
  res.json({
    lanes: latestSignalState.lane_weights,
    densities: latestSignalState.lane_densities,
    emergency: latestSignalState.emergency_active,
    active_lane: latestSignalState.active_lane,
  });
});

// ──────────────────────────────────────────────
// Socket.io
// ──────────────────────────────────────────────
io.on("connection", (socket) => {
  console.log(`[SOCKET] Client connected: ${socket.id}`);

  // Send the current state immediately on connect
  socket.emit("signal:update", {
    ...latestSignalState,
    override: manualOverride,
  });

  socket.on("disconnect", () => {
    console.log(`[SOCKET] Client disconnected: ${socket.id}`);
  });
});

// Countdown ticker — pushes seconds_remaining every second
setInterval(() => {
  if (manualOverride.active && manualOverride.expiresAt < Date.now()) {
    // Override has expired — clear it
    manualOverride = {
      active: false,
      lane: null,
      duration: null,
      expiresAt: null,
    };
    io.emit("signal:override_cancelled", { reason: "expired" });
  }

  const payload = {
    ...latestSignalState,
    seconds_remaining: Math.max(
      0,
      parseFloat((latestSignalState.seconds_remaining - 1).toFixed(1)),
    ),
    override: manualOverride,
  };

  // If override is active, force the payload values to reflect it
  if (manualOverride.active) {
    payload.active_lane = manualOverride.lane;
    payload.seconds_remaining = Math.max(
      0,
      parseFloat(((manualOverride.expiresAt - Date.now()) / 1000).toFixed(1))
    );
  }

  latestSignalState.active_lane = payload.active_lane;
  latestSignalState.seconds_remaining = payload.seconds_remaining;
  io.emit("signal:tick", payload);
}, 1000);

// ──────────────────────────────────────────────
// ZeroMQ subscriber — connects to optimizer.py
// ──────────────────────────────────────────────
async function startZmqSubscriber() {
  const sock = new zmq.Subscriber();
  sock.connect(ZMQ_SUB_ADDR);
  sock.subscribe("signal");
  console.log(`[ZMQ] Subscribed to optimizer at ${ZMQ_SUB_ADDR}`);

  for await (const [topic, msg] of sock) {
    try {
      const data = JSON.parse(msg.toString());

      // Respect active manual override — don't overwrite active_lane
      if (manualOverride.active) {
        data.active_lane = manualOverride.lane;
        data.seconds_remaining = Math.max(
          0,
          (manualOverride.expiresAt - Date.now()) / 1000,
        );
      }

      latestSignalState = { ...data, timestamp: data.timestamp };
      io.emit("signal:update", {
        ...latestSignalState,
        override: manualOverride,
      });
    } catch (err) {
      console.error("[ZMQ] JSON parse error:", err.message);
    }
  }
}

// ──────────────────────────────────────────────
// Boot
// ──────────────────────────────────────────────
server.listen(PORT, () => {
  console.log(`\n┌──────────────────────────────────────────────┐`);
  console.log(`│  AI Traffic Optimizer — Command Center API    │`);
  console.log(`│  Listening on http://localhost:${PORT}           │`);
  console.log(`│  ZeroMQ SUB  → ${ZMQ_SUB_ADDR}        │`);
  console.log(`│  Press Ctrl+C to stop                         │`);
  console.log(`└──────────────────────────────────────────────┘\n`);
  startZmqSubscriber().catch((err) => {
    console.error("[ZMQ] Fatal subscriber error:", err);
    process.exit(1);
  });
});

// Graceful shutdown
process.on("SIGTERM", () => {
  server.close(() => process.exit(0));
});
process.on("SIGINT", () => {
  server.close(() => process.exit(0));
});
