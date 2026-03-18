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

## Solar Forecast Inaccurate

### Always Overestimating

- Check the Cloud Coverage sensor's `cloud_source` attribute
  - `open-meteo_layers` is best (per-altitude weighting)
  - `met.no_total` is fallback (less accurate, treats all cloud types equally)
- Check `effective_cloud_pct` -- high cirrus (which barely blocks solar) should show low effective cloud, not 100%
- The mid-day adjustment (after 10am) should downgrade the day type if actual production is below 60% of expected

### Always Underestimating

- Check if VRM API is working (Solar Forecast Today sensor's `solar_forecast_source`)
  - `clearsky_p90` through `clearsky_p15` means VRM data is being used (good)
  - `ha_history` or `bell_curve` means VRM is unavailable (less accurate)
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
