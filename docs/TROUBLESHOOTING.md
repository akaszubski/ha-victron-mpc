# Troubleshooting

## Integration Won't Load

### Symptoms
- Integration shows as "failed to set up" in Settings > Devices & Services
- No entities appear
- HA logs show import errors

### Possible Causes

**A. scipy not installed or wrong version**

The integration requires `scipy >= 1.12.0` and `numpy >= 1.26.0`. On some HA installations (especially HAOS on Raspberry Pi), these may fail to install or take a long time.

Check the logs for:
```
ModuleNotFoundError: No module named 'scipy'
```
or
```
ImportError: scipy requires numpy >= 1.26.0
```

**Solution**: The integration declares these in `manifest.json` and HA should install them automatically. If it fails:

1. Check HA logs for pip install errors
2. On HAOS, large packages like scipy can take 5-10 minutes to compile on first install -- wait and check again
3. If on a Raspberry Pi 3 or older, scipy compilation may fail due to limited RAM. Consider upgrading to a Pi 4 or x86 system.

**B. Modbus integration not configured**

The integration depends on the HA Modbus integration. If it is not configured, you will see:
```
DependencyError: Dependencies ['modbus'] not found
```

**Solution**: Configure the Modbus integration first (Settings > Integrations > Add > Modbus).

**C. Config entry corrupt**

If the config flow was interrupted:

**Solution**: Remove the integration (Settings > Devices & Services > Victron MPC > Delete) and re-add it.

---

## Sensors Show "Unknown" or "Unavailable"

### After First Setup

The coordinator needs one successful 5-minute cycle to populate all sensors. Wait at least 5 minutes after setup, then check:

1. **Battery Plan** sensor -- should show a numeric SoC target
2. Check logs for `MPC cycle 1:` messages -- this confirms the optimizer ran

### After a Period of Working

Check the **Data Stale** binary sensor. If it is `on`, the coordinator is failing.

Common causes:
- Amber Electric integration went offline (check `sensor.amber_general_price`)
- Modbus connection lost (check modbus integration status)
- VRM API token expired (check logs for 401 errors)

**Solution**: Fix the upstream issue. The coordinator retries every 5 minutes automatically.

---

## Battery Not Charging/Discharging as Expected

### 1. Check Shadow Mode

If Shadow Mode is ON, the integration is not writing registers. Check:
- **Shadow Mode** switch entity -- should be OFF for live operation
- Logs should show `R2901 written:` (not `SHADOW: Would write`)

### 2. Check Register Values

Verify what was actually written:
- Battery Plan sensor's `target_register` attribute shows what the integration wants
- Check the Modbus register readback sensor (if configured) for what the inverter actually has

### 3. Check Override Active

The **Spike Override Active** binary sensor shows if a spike override is in effect. During a spike, the integration forces R2901=100 (discharge to 10%) regardless of what the LP computed.

Check the Decision sensor's `override_applied` and `override_reason` attributes.

### 4. Check ESS Assistant

The Victron ESS Assistant must be installed and configured. Without it, register writes have no effect. Verify in VRM > Device List > Inverter > Settings.

---

## Modbus Write Failing

### Symptoms
- Logs show `Failed to write R2901=XXX`
- Battery behavior does not change
- Modbus errors in HA logs

### Solutions

1. **Verify Cerbo GX is reachable**: Can you ping the IP address?
2. **Verify Modbus TCP is enabled**: Check Cerbo GX > Settings > Services > Modbus TCP
3. **Verify unit ID**: The default system unit ID is 100. Check your Cerbo GX documentation if you have a non-standard setup
4. **Check for conflicting writes**: If another automation or integration is also writing to R2901/R2706, they will conflict. Disable any other battery management automations
5. **Test manual write**: In HA Developer Tools > Services, try:
   ```yaml
   service: modbus.write_register
   data:
     hub: victron  # or your hub name
     unit: 100
     address: 2901
     value: 500
   ```

---

## Amber API Down

### Symptoms
- Amber price entity shows `unavailable` or `unknown`
- Decision sensor shows `override_applied: true` with reason mentioning "defensive mode"
- Persistent notification: "MPC: Amber Pricing Unavailable"

### What Happens

When the Amber API is unavailable for more than the configured **Amber Blip Minutes** (default 5 minutes), the integration activates **defensive discharge** mode:

- **17:00-21:00 (evening peak)**: Assumes the configured **Defensive Price** (default $2.00/kWh) -- the optimizer discharges the battery to avoid potential spike-rate grid import
- **All other hours**: Assumes the configured **Fallback Price** (default $0.30/kWh) -- conservative hold to preserve battery

This is intentionally aggressive during evening peak because that is when the most expensive spikes occur. Missing a $5-25/kWh spike while running on grid power could cost $10-50 in a single hour.

All three parameters are adjustable:
- **Amber Blip Minutes** (`number.victron_mpc_battery_optimizer_amber_blip_minutes`): Increase to tolerate longer Amber glitches before defensive mode activates
- **Defensive Price** (`number.victron_mpc_battery_optimizer_defensive_price`): Raise for more aggressive defensive discharge, lower if your area has milder spikes
- **Fallback Price**: Adjustable in the options flow

**Tip**: If defensive discharge triggers too often due to brief Amber outages, increase the Amber Blip Minutes from 5 to 10 or 15. This gives Amber more time to recover from transient glitches before the integration switches to defensive pricing.

### Solutions

1. **Check the Amber integration**: Go to Settings > Integrations > Amber Electric and verify it is connected
2. **Check Amber's status page**: Amber may have a service outage
3. **Check your internet connection**: Amber requires internet access
4. **Wait**: Brief Amber outages (under the configured blip minutes) use the last known price and do not trigger defensive mode. The integration recovers automatically when Amber returns.
5. **Adjust tolerance**: If Amber has frequent brief outages in your area, increase Amber Blip Minutes to reduce false defensive triggers

### After Recovery

When the Amber entity returns to a valid state, the integration immediately:
- Switches back to real Amber pricing
- Clears the defensive mode
- Dismisses the persistent notification on the next successful cycle

---

## Modbus Communication Failed

### Symptoms
- `binary_sensor.victron_mpc_battery_optimizer_modbus_connected` is OFF
- Persistent notification: "MPC: Modbus Communication Failed"
- Logs show `Failed to write R2901=XXX` or `Failed to write R2706=XXX`
- Battery behavior does not change despite different MPC decisions

### What Happens

After 3 consecutive failed register writes, the integration marks Modbus as unhealthy. The registers remain at their **last successfully written values** -- the integration cannot change the battery's behavior until Modbus is restored.

### Solutions

1. **Ping the Cerbo GX**: `ping 192.168.0.197` (your Cerbo IP) -- if unreachable, it is a network issue
2. **Check Cerbo GX is powered**: Physical inspection, or check VRM portal
3. **Verify Modbus TCP is enabled**: Cerbo GX > Settings > Services > Modbus TCP
4. **Check the HA Modbus integration**: Settings > Integrations > Modbus -- it should be online
5. **Restart the Modbus integration**: Sometimes the TCP connection drops. Disabling and re-enabling the Modbus integration in HA can restore it
6. **Check for IP changes**: If the Cerbo GX got a new DHCP address, the Modbus integration will fail. Consider assigning a static IP or DHCP reservation

### After Recovery

When register writes succeed again, the integration automatically:
- Resets the failure counter
- Sets the Modbus Connected binary sensor back to ON
- Sends a "Modbus Communication Restored" persistent notification

---

## Internet Outage

### Symptoms
- Multiple API-dependent sensors may show degraded data
- `solar_forecast_source` attribute changes to `ha_history` or `bell_curve`
- Cloud coverage falls back to `met.no_total` source
- Amber prices may go stale

### What Happens

The integration degrades gracefully through its fallback chains:

**Solar forecast fallback chain:**
1. Solcast -- unavailable (requires internet)
2. VRM cache -- available for ~24h from last successful fetch
3. **HA recorder history** -- uses 7 days of local solar sensor data to build an hourly profile
4. Synthetic bell curve -- last resort if recorder has no data

**Load forecast fallback chain:**
1. VRM consumption forecast -- unavailable (requires internet)
2. **HA recorder history** -- uses 7 days of local load sensor data
3. Typical residential curve -- last resort

**Price forecast:**
- Amber goes stale after the last received forecast expires
- Defensive discharge activates after 5 minutes (see "Amber API Down" above)

**Cloud/weather:**
- Open-Meteo unavailable, met.no weather entity may use cached forecast
- Solar derating falls back to weather entity's total cloud percentage

### The Key Point

With HA recorder history enabled (default in HA), the integration can operate for extended periods without internet. The forecasts are less accurate than Solcast or VRM, but they capture your site's actual solar/load patterns from recent days.

### Improving Offline Resilience

Ensure your HA recorder retains at least 7 days of history:

```yaml
# configuration.yaml
recorder:
  purge_keep_days: 10
```

See [SETUP.md](SETUP.md) for more details on recorder configuration.

---

## How to Test Failure Scenarios

The integration has a comprehensive 48-scenario failure matrix documented in [TEST_SCENARIOS.md](TEST_SCENARIOS.md). Here are practical ways to test key scenarios:

### Simulated API Failures (Safe in Shadow Mode)

| Scenario | How to Simulate |
|----------|----------------|
| Amber down | Disable the Amber Electric integration temporarily |
| VRM down | Remove VRM token from the MPC config entry options |
| Solcast down | Disable the ha-solcast-solar integration |
| Internet down | Block outbound traffic on your Pi's firewall |
| Modbus down | Disable the Modbus integration |

### What to Observe

After simulating a failure, check:
1. **Decision sensor** -- `solar_forecast_source` and `override_reason` should reflect the fallback
2. **Solar Forecast Today** -- `solar_forecast_source` attribute shows which level of the chain is active
3. **Binary sensors** -- Data Stale, Modbus Connected should reflect the failure state
4. **HA logs** -- filter for `victron_mpc` to see fallback chain logging
5. **Persistent notifications** -- Amber down and Modbus failure notifications should appear

### Automated Test Suite

The integration includes 178 tests covering these failure scenarios:

```bash
cd /tmp/ha-victron-mpc
python -m pytest tests/ -v
```

---

## Solcast Issues

### Solcast Entity Not Found

If `solar_forecast_source` never shows `solcast_ha` even though you installed ha-solcast-solar:

1. **Verify the entity exists**: Check that `sensor.solcast_pv_forecast_forecast_today` appears in **Developer Tools** > **States**
2. **Check the entity name**: If you renamed the entity or have multiple Solcast configurations, the auto-detection may not find it. Select the correct entity in the MPC config flow Step 4 (Victron Sensors > Solcast Forecast Entity)
3. **Verify the attribute**: The entity must have a `detailedForecast` attribute containing an array of 30-minute forecast entries with `pv_estimate` fields
4. **Restart HA**: After installing ha-solcast-solar, a full HA restart is required for the entity to become available

### Solcast Rate Limits

The free Solcast hobbyist account allows 10 API calls per day. If you exceed this:

- Solcast data goes stale (last successful fetch continues to be used until midnight)
- MPC automatically falls through to VRM-based forecasting when Solcast data is too old
- The `solar_forecast_source` attribute will change from `solcast_ha` to a VRM source
- Consider spacing Solcast update automations to every 2-3 hours (4-6 calls during daylight covers the day well)

### Solcast Data Looks Wrong

1. **Check rooftop config**: Log in to [toolkit.solcast.com.au](https://toolkit.solcast.com.au/) and verify panel orientation, tilt, capacity, and location
2. **Compare with actuals**: Check `solar_forecast_source: solcast_ha` on the Solar Forecast Today sensor and compare the forecast kWh with actual production over several days
3. **Multiple roof planes**: If you have panels on multiple roof faces, ensure all are configured in Solcast. The ha-solcast-solar integration aggregates them automatically

---

## Solar Forecast Inaccurate

### Always Overestimating

- Check the Cloud Coverage sensor's `cloud_source` attribute
  - `open-meteo_layers` is best (per-altitude weighting)
  - `met.no_total` is fallback (less accurate, treats all cloud types equally)
- Check `effective_cloud_pct` -- high cirrus (which barely blocks solar) should show low effective cloud, not 100%
- The mid-day adjustment (after 10am) should downgrade the day type if actual production is below 60% of expected

### Always Underestimating

- Check if Solcast is available (Solar Forecast Today sensor's `solar_forecast_source`)
  - `solcast_ha` means Solcast is active (best accuracy)
  - `clearsky_p90` through `clearsky_p15` means VRM data is being used (good)
  - `ha_history` or `bell_curve` means both Solcast and VRM are unavailable (less accurate)
- If Solcast is installed but not being used, see the "Solcast Issues" section above
- Verify your VRM token has not expired

### No VRM Data

If `solar_forecast_source` shows `ha_history` or `bell_curve`:

1. Check that VRM token and installation ID were entered during setup
2. Check logs for VRM API errors (401 = token expired, 403 = wrong installation ID)
3. Generate a new token at [vrm.victronenergy.com](https://vrm.victronenergy.com) > Profile > Access Tokens

---

## High CPU or Slow Performance

The LP solver runs in an executor thread and should complete in ~50ms. Check the **Solver Time** sensor.

If solver time is consistently above 200ms:
- This is unusual but not harmful (the 5-minute interval provides ample headroom)
- On very constrained hardware (Pi 3), scipy may be slower

If HA becomes sluggish after installing the integration:
- Check that only one instance of the integration is configured
- Check logs for rapid error/retry cycles (exponential backoff should prevent this, but verify)

---

## Debug Logging

To enable detailed logging, add to your `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.victron_mpc: debug
```

Then restart HA. Debug logs will show:
- Every optimization cycle with inputs and outputs
- Register write decisions and values
- Override trigger reasons
- API fetch success/failure for VRM, Open-Meteo, PetrolSpy
- Forecast source selection and fallback chain

To return to normal logging, remove the `custom_components.victron_mpc: debug` line and restart.

---

## Diagnostics Dump

The integration supports the HA diagnostics feature:

1. Go to **Settings** > **Devices & Services** > **Victron MPC Battery Optimizer**
2. Click the three dots menu > **Download diagnostics**

The diagnostics dump includes:
- Current config entry data (with tokens redacted)
- Current options/tunables
- Latest coordinator data (all sensor values)
- API health status
- Cycle count and failure tracking

Include this dump when reporting issues.

---

## Rollback / Disabling

### Temporary Disable (Keep Config)

1. Turn **Shadow Mode** ON -- stops register writes immediately
2. The integration continues computing but does not affect your system

### Full Disable

1. Go to **Settings** > **Devices & Services** > **Victron MPC Battery Optimizer**
2. Click the three dots menu > **Disable**
3. The coordinator stops, no more cycles run
4. Your Victron system reverts to its last register values

### Uninstall

1. Go to **Settings** > **Devices & Services** > **Victron MPC Battery Optimizer**
2. Click **Delete**
3. Optionally remove the integration files from `custom_components/victron_mpc/`
4. Restart HA

After disabling or uninstalling, manually set R2901 to your preferred default via Developer Tools > Services:

```yaml
service: modbus.write_register
data:
  hub: victron
  unit: 100
  address: 2901
  value: 500   # 50% - safe default
```

---

## Known Limitations

### scipy on Raspberry Pi

scipy is a large package that requires compilation on ARM. On a Raspberry Pi 3 (1GB RAM), compilation may fail. Recommended minimum: Raspberry Pi 4 (2GB+) or x86 hardware.

### Amber Forecast Horizon

Amber provides ~30 hours of price forecast. Beyond that, the LP uses the last known price. This means decisions for the far end of the 24h horizon are less reliable.

### Modbus Write Latency

Register changes are not instant. The Victron ESS system may take 1-2 cycles (5-10 seconds) to react to a new register value.

### Battery Behavior Near 100%

Victron often stops charging at 95-98% due to absorption phase behavior. The optimizer accounts for this but cannot force the battery above what the BMS allows.

### Single Instance

Only one integration instance per HA installation is supported. If you have multiple Victron systems, only one can be managed by this integration.
