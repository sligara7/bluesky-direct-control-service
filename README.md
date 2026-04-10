# Direct Device Control Service (SVC-003)

Device commanding with A4 coordination checks for Bluesky Remote Architecture.

## Features

- **A4 Device Coordination**: Checks with experiment_execution before commanding (returns 423 if locked)
- **PV Control**: Low-fidelity channel for EPICS PV set/get (fire-and-forget or put-completion)
- **Device Method Execution**: High-fidelity channel for Ophyd device methods
- **Nested Device Access**: Navigate device component hierarchies (ophyd-websocket compatible)
- **WebSocket Control**: Real-time device control protocol
- **Authorization**: RBAC integration with auth_service

## Deployment

### Installation

```bash
# From the service directory
pip install -e .

# Or from the monorepo
pip install -e archive/services/direct_control/
```

### Running the Service

```bash
# Basic startup
bluesky-direct-control

# With custom experiment_execution URL
bluesky-direct-control --port 8003
export DIRECT_CONTROL_EXPERIMENT_EXECUTION_URL=http://localhost:8001

# Disable coordination checks (testing)
DIRECT_CONTROL_COORDINATION_CHECK_ENABLED=false bluesky-direct-control

# Disable auth requirement (testing)
DIRECT_CONTROL_REQUIRE_AUTH=false bluesky-direct-control

# Development mode with auto-reload
bluesky-direct-control --reload --log-level debug
```

### Docker

```bash
docker build -t bluesky-direct-control .
docker run -p 8003:8003 \
  -e DIRECT_CONTROL_EXPERIMENT_EXECUTION_URL=http://host.docker.internal:8001 \
  bluesky-direct-control
```

## Service Dependencies

| Dependency | Interface | Purpose |
|------------|-----------|---------|
| `experiment_execution` | `/api/v1/coordination/devices/{name}/status` | A4 device lock status |
| `configuration_service` | `/api/v1/devices/{name}` | Device metadata |
| `auth_service` | `/api/v1/auth/validate` | Token validation |

## Configuration

All settings use the `DIRECT_CONTROL_` environment variable prefix.

| Variable | Default | Description |
|----------|---------|-------------|
| `DIRECT_CONTROL_HOST` | `0.0.0.0` | Bind address |
| `DIRECT_CONTROL_PORT` | `8003` | HTTP port |
| `DIRECT_CONTROL_LOG_LEVEL` | `info` | Log level |
| `DIRECT_CONTROL_EXPERIMENT_EXECUTION_URL` | `http://localhost:8001` | Experiment Execution URL |
| `DIRECT_CONTROL_CONFIGURATION_SERVICE_URL` | `http://localhost:8004` | Configuration Service URL |
| `DIRECT_CONTROL_AUTH_SERVICE_URL` | `http://localhost:8010` | Auth Service URL |
| `DIRECT_CONTROL_COORDINATION_CHECK_ENABLED` | `true` | Enable A4 coordination checks |
| `DIRECT_CONTROL_COORDINATION_TIMEOUT` | `5.0` | Coordination check timeout (seconds) |
| `DIRECT_CONTROL_REQUIRE_AUTH` | `true` | Require authorization token |
| `DIRECT_CONTROL_ALLOWED_ROLES` | `["staff", "scientist"]` | Roles allowed to command |
| `DIRECT_CONTROL_COMMAND_TIMEOUT` | `30.0` | Command execution timeout (seconds) |
| `DIRECT_CONTROL_ENABLE_METRICS` | `true` | Enable Prometheus metrics |
| `DIRECT_CONTROL_METRICS_PORT` | `9003` | Metrics port |
| `EPICS_CA_ADDR_LIST` | - | EPICS Channel Access address list |
| `EPICS_CA_AUTO_ADDR_LIST` | `YES` | Auto-discover EPICS addresses |

### CLI Options

```
bluesky-direct-control --help

Options:
  --host TEXT                     Host to bind to (default: 0.0.0.0)
  --port INTEGER                  Port to bind to (default: 8003)
  --reload                        Enable auto-reload for development
  --workers INTEGER               Number of worker processes
  --log-level [critical|error|warning|info|debug|trace]
```

## API Endpoints

### Health & Status
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check (includes coordination status) |
| GET | `/api/v1/stats` | Service statistics |

### PV Control (Low Fidelity)
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/pv/set` | Set PV value (coordination check) |
| GET | `/api/v1/pv/{pv_name}/value` | Get PV value (read-only, no coordination) |

### Device Control (High Fidelity)
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/device/execute` | Execute Ophyd device method |

### Nested Device Access (ophyd-websocket compatible)
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/device/{device_path}` | Access nested component (read/set) |
| GET | `/api/v1/device/{device_path}/value` | Get nested component value |

### WebSocket Control
| Method | Endpoint | Description |
|--------|----------|-------------|
| WS | `/api/v1/control-socket` | Real-time device control |

## Example curl Commands

### Health Check
```bash
curl http://localhost:8003/health
```

### Set PV Value (Fire-and-Forget)
```bash
curl -X POST http://localhost:8003/api/v1/pv/set \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"pv_name": "IOC:motor1", "value": 10.0, "wait": false}'
```

### Set PV Value (Put-Completion)
```bash
curl -X POST http://localhost:8003/api/v1/pv/set \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"pv_name": "IOC:motor1", "value": 10.0, "wait": true, "timeout": 5.0}'
```

### Get PV Value (Read-Only)
```bash
curl http://localhost:8003/api/v1/pv/IOC:motor1/value
```

### Execute Device Method
```bash
curl -X POST http://localhost:8003/api/v1/device/execute \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"device_name": "det", "method": "trigger", "args": [], "kwargs": {}}'
```

### Access Nested Device Component (Read)
```bash
curl -X POST http://localhost:8003/api/v1/device/motor.user_readback \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"method": "read"}'
```

### Access Nested Device Component (Set)
```bash
curl -X POST http://localhost:8003/api/v1/device/motor.user_setpoint \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"method": "set", "value": 5.0}'
```

### Get Nested Device Value (Read-Only)
```bash
curl http://localhost:8003/api/v1/device/motor.user_readback/value
```

### Get Statistics
```bash
curl http://localhost:8003/api/v1/stats
```

## WebSocket Protocol

Connect to `/api/v1/control-socket` for real-time device control:

```python
import asyncio
import websockets
import json

async def control_device():
    uri = "ws://localhost:8003/api/v1/control-socket"
    async with websockets.connect(uri) as ws:
        # Set PV value
        await ws.send(json.dumps({
            "action": "set",
            "pv": "IOC:motor1",
            "value": 10.0
        }))
        response = await ws.recv()
        print(json.loads(response))

        # Set device component
        await ws.send(json.dumps({
            "action": "set",
            "device": "motor",
            "component": "user_setpoint",
            "value": 5.0
        }))
        response = await ws.recv()
        print(json.loads(response))

        # Get PV value
        await ws.send(json.dumps({
            "action": "get",
            "pv": "IOC:motor1"
        }))
        response = await ws.recv()
        print(json.loads(response))

asyncio.run(control_device())
```

### WebSocket Messages

**Client to Server:**
```json
{"action": "set", "pv": "IOC:m1", "value": 10}
{"action": "set", "device": "motor1", "component": "user_setpoint", "value": 10}
{"action": "get", "pv": "IOC:m1"}
{"action": "get", "device": "motor1", "component": "user_readback"}
{"action": "ping"}
```

**Server to Client:**
```json
{"type": "set_complete", "pv": "...", "success": true, "value": 10}
{"type": "value", "pv": "...", "value": 10.5, "timestamp": "..."}
{"type": "pong", "timestamp": "..."}
{"type": "error", "message": "Device locked by plan count", "locked": true}
```

## A4 Coordination

The service implements the **A4 coordination requirement**:

1. Before any write operation, check with `experiment_execution` if the device is locked
2. If device is locked by a running plan, return `HTTP 423 Locked`
3. If coordination service unavailable, return `HTTP 503 Service Unavailable`

```
User Request → Authorization Check → Coordination Check → EPICS Command
                                           ↓
                                   Device locked?
                                   Yes → 423 Locked
                                   No  → Execute
```
