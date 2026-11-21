# EUM BMS PRO — 이음BMS

Initial development build (v0.1.0). Not production-ready.

## Run

```bash
docker compose up --build
```

Open http://localhost:8000 and http://localhost:8000/docs.

## Implemented
- Site, Modbus TCP device and holding-register tag CRUD (create/list)
- PostgreSQL persistence (SQLite fallback for local development)
- Read-only Modbus TCP polling code, disabled by default
- Quality flags and reading persistence
- Basic Korean web dashboard

## NOT implemented / safety limitations
- Authentication, tenant isolation, TLS, secrets management, migrations, gateway, high availability, audit logs
- Production-grade Modbus compatibility validation and hardware-in-the-loop tests
- BACnet, OPC UA, other manufacturer drivers, remote metering and tariffs, diagrams, alarms
- Remote control is intentionally absent
- Polling must remain disabled on public servers. Enable only in isolated, authorized test networks after authentication and network access controls are implemented.

Do not expose this prototype publicly or connect it to operational power infrastructure.
