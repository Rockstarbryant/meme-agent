# REST API (control plane)
Generated from the OpenAPI schema (interactive docs at `/docs`).
User routes: `Authorization: Bearer <JWT>`. Runner routes (`/runner/*`): `Authorization: Bearer rt_<runner token>` (issued by pairing; useless elsewhere).
SSE: `POST /stream/ticket` then `GET /stream?ticket=...` (60s single-use ticket).

| Method | Path | Tag | Summary |
|---|---|---|---|
| GET | `/activity` | trading | Activity |
| GET | `/agent` | agent | Get Agent |
| POST | `/agent/close-all` | agent | Close All |
| POST | `/agent/close/{position_id}` | agent | Close Position |
| POST | `/agent/emergency-stop` | agent | Emergency |
| POST | `/agent/mode` | agent | Set Mode |
| POST | `/agent/pause` | agent | Pause |
| POST | `/agent/start` | agent | Start |
| POST | `/agent/stop` | agent | Stop |
| GET | `/audit-logs` | trading | Audit Logs |
| POST | `/auth/login` | auth | Login |
| POST | `/auth/logout` | auth | Logout |
| GET | `/auth/me` | auth | Me |
| POST | `/auth/register` | auth | Register |
| GET | `/chains` | system | Chains |
| GET | `/controls/blacklist` | config | Get Blacklist |
| POST | `/controls/blacklist` | config | Add Blacklist |
| DELETE | `/controls/blacklist` | config | Remove Blacklist |
| GET | `/decisions/{decision_id}` | trading | Decision Detail |
| GET | `/health` | system | Health |
| GET | `/health/live` | system | Live |
| GET | `/launchpads` | system | Launchpads |
| GET | `/opportunities` | trading | Opportunities |
| GET | `/orders` | trading | Orders |
| GET | `/portfolio` | trading | Portfolio |
| GET | `/positions` | trading | Positions |
| GET | `/risk/limits` | config | Get Limits |
| PUT | `/risk/limits` | config | Put Limits |
| POST | `/runner/commands/{cid}/ack` | runner (used by the Local Runner) | Ack Command |
| GET | `/runner/config` | runner (used by the Local Runner) | Get Config |
| POST | `/runner/events` | runner (used by the Local Runner) | Post Events |
| POST | `/runner/heartbeat` | runner (used by the Local Runner) | Heartbeat |
| POST | `/runner/pair` | runner (used by the Local Runner) | Pair |
| GET | `/runners` | runners | List Runners |
| POST | `/runners/pairing-codes` | runners | Create Pairing Code |
| DELETE | `/runners/{runner_id}` | runners | Revoke Runner |
| GET | `/settings` | config | Settings |
| GET | `/strategies` | config | Strategies |
| PUT | `/strategies/{sid}/enabled` | config | Set Enabled |
| GET | `/strategies/{sid}/versions` | config | Versions |
| POST | `/strategies/{sid}/versions` | config | New Version |
| GET | `/stream` | stream | Stream |
| POST | `/stream/ticket` | stream | Ticket |
| GET | `/tokens/{token_key}` | trading | Token Detail |
| GET | `/wallet` | wallet | Wallet State |
| GET | `/wallet/activity` | wallet | Wallet Activity |
| POST | `/wallet/authorize` | wallet | Authorize |
| POST | `/wallet/challenge` | wallet | Challenge |
| POST | `/wallet/connect` | wallet | Connect |
| POST | `/wallet/policy` | wallet | Set Policy |
| POST | `/wallet/revoke` | wallet | Revoke |
| GET | `/wallet/signing-requests` | wallet | Signing Requests |
| POST | `/wallet/signing-requests/{rid}/complete` | wallet | Complete Signing |
