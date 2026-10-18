# Scheduling the quota refresh

`llm-router quota refresh --once` does one serial pass. It refreshes only the meters that are due
(`quota_refresh.interval_seconds`), backs off on failure, skips a pool at 100% until its reset, and raises
alerts. It holds a lock, so overlapping runs are harmless. Run it every `quota_refresh.agent_period_seconds`
(default 5 min). Without a scheduler the router still works: `select`/`run-task` read a stale meter live once.

## macOS (LaunchAgent)

```bash
llm-router install-agent          # dry run: prints the plist and the launchctl commands
llm-router install-agent --yes    # writes ~/Library/LaunchAgents/dev.llm-router.quota.plist and bootstraps it
llm-router uninstall-agent --yes
```

The label is `launch_agent.label` (default `dev.llm-router.quota`). The plist records the resolved `llm-router`
path, a PATH that finds the worker CLIs, and any `LLM_ROUTER_*` / `XDG_*` variables set at install time.
`install-agent` refuses to overwrite an existing plist. Long jobs use `caffeinate -i` on macOS
(`run_job.caffeinate`).

## Linux (systemd user timer)

```bash
mkdir -p ~/.config/systemd/user
cp contrib/systemd/llm-router-quota.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now llm-router-quota.timer
journalctl --user -u llm-router-quota.service -n 20
```

Edit `ExecStart` and `PATH` in the service if `llm-router` or the worker CLIs live elsewhere. For timers to
run while you are logged out: `loginctl enable-linger $USER`.

## cron (any Unix)

```cron
*/5 * * * * PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin llm-router quota refresh --once >/dev/null 2>&1
```

## Checking

```bash
llm-router status     # meters, age, resets, eligibility, open alerts
llm-router alerts     # list; `llm-router alerts ack --id <id>` to acknowledge
```
