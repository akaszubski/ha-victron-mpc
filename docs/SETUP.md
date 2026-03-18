# Setup Guide

## Prerequisites Checklist

Before installing, confirm the following:

- [ ] **Home Assistant 2024.8+** running
- [ ] **Victron Cerbo GX** (or Venus GX) on your local network
- [ ] **Modbus TCP enabled** on the Cerbo GX (see below)
- [ ] **HA Modbus integration** configured and connected to the Cerbo GX
- [ ] **Amber Electric integration** configured in HA with price, forecast, and spike entities
- [ ] **Victron sensor entities** available in HA (battery SoC, solar power, AC consumption, grid power)
- [ ] **Weather integration** configured (met.no `weather.home` or similar)
- [ ] **(Optional)** VRM account with API access token

## Enabling Modbus TCP on Cerbo GX

1. Access the Cerbo GX remote console (VRM portal or local IP)
2. Go to **Settings** > **Services** > **Modbus TCP**
3. Set **Modbus TCP** to **Enabled**
4. Note the IP address of your Cerbo GX (e.g., `192.168.0.197`)
5. The default Modbus port is **502**

Unit IDs for most single-inverter setups:
- **System unit ID**: 100 (aggregate system data)
- **VE.Bus unit ID**: 227 (MultiPlus/Quattro inverter)

If you have multiple inverters, check VRM > Device List for your specific unit IDs.

## Getting a VRM API Token

VRM access is optional but strongly recommended. It provides 180 days of historical solar production data, which the integration uses to build weather-classified solar forecasts.

1. Go to [https://vrm.victronenergy.com](https://vrm.victronenergy.com) and log in
2. Click your profile icon (top right) > **Preferences**
3. Scroll to **Access Tokens** (or **Security / API Access**)
4. Click **Add** or **Create Token**
5. Name it `Home Assistant MPC` (or similar)
6. Copy the generated token -- it will look like `eyJhbGciOiJIUzI1NiIs...`
7. Save it securely; you cannot view it again after closing the dialog

To find your **Installation ID**:
1. In VRM, navigate to your installation
2. The URL will show `https://vrm.victronenergy.com/installation/XXXXX/...`
3. The number (`XXXXX`) is your Installation ID

## Config Flow Walkthrough

After installing the integration, go to **Settings** > **Devices & Services** > **Add Integration** and search for **Victron MPC Battery Optimizer**.

### Step 1: Victron Modbus Connection

| Field | Default | Description |
|-------|---------|-------------|
| Cerbo GX IP Address | `192.168.0.197` | Local IP of your Cerbo GX |
| Modbus TCP Port | `502` | Standard Modbus port |
| System Unit ID | `100` | Aggregate system metrics |
| VE.Bus Unit ID (Inverter) | `227` | Your MultiPlus/Quattro |

The integration uses the HA Modbus integration to write registers, so ensure that integration is already configured and connected.

### Step 2: Battery System

| Field | Default | Description |
|-------|---------|-------------|
| Battery Capacity (kWh) | `14.2` | Total usable capacity of your battery bank |
| Max Charge Rate (kW) | `3.5` | Maximum grid-to-battery charge rate |
| Max Discharge Rate (kW) | `4.5` | Maximum battery-to-load discharge rate |
| Minimum SoC Floor (%) | `20` | Lowest the optimizer will plan to discharge during the day |

Set these to match your actual hardware. Undersizing charge/discharge rates is safer than oversizing. If unsure, check VRM > Advanced > Battery for observed rates.

### Step 3: Amber Electric

Select the Amber Electric entities from your existing HA Amber integration:

| Field | Default Entity | Description |
|-------|---------------|-------------|
| General Price | `sensor.amber_general_price` | Current wholesale buy price |
| Price Forecast | `sensor.amber_general_forecast` | 30h price forecast array |
| Feed-in Price | `sensor.amber_feed_in_price` | Current feed-in tariff |
| Feed-in Forecast | `sensor.amber_feed_in_forecast` | Feed-in price forecast |
| Price Spike Sensor | `binary_sensor.amber_price_spike` | Amber spike detection |

If your Amber entities have different names (e.g., you renamed them), use the entity picker to select the correct ones.

### Step 4: Victron Sensors

Select the Victron entities from your existing HA Modbus integration:

| Field | Default Entity | Description |
|-------|---------------|-------------|
| Battery State of Charge | `sensor.victron_battery_state_of_charge` | Battery percentage |
| Solar Power (PV) | `sensor.solar_power` | Current solar production in watts |
| AC Consumption (Load) | `sensor.victron_ac_consumption` | Current household load in watts |
| Grid Power | `sensor.victron_grid_power` | Current grid import/export in watts |
| Generator Power (optional) | -- | Genset power if you have one |
| Weather Entity | `weather.home` | Weather forecast for cloud data |

### Step 5: VRM API (Optional)

| Field | Default | Description |
|-------|---------|-------------|
| VRM Access Token | (empty) | Token from the VRM portal (see above) |
| VRM Installation ID | (empty) | Numeric installation ID from VRM URL |

Leave both blank if you do not have a VRM account. The integration will fall back to HA sensor history for solar forecasting, which is less accurate but functional.

## Post-Setup Verification

After completing the config flow, the integration creates a **Victron MPC Battery Optimizer** device with all entities. Here is what to check:

### 1. Entities are available

Go to **Settings** > **Devices & Services** > **Victron MPC Battery Optimizer** and verify:

- 11 sensor entities (Battery Plan, Decision, Effective Price, etc.)
- 6 number entities (Battery Wear Cost, Sunset Reward, etc.)
- 1 switch entity (Shadow Mode)
- 2 binary sensor entities (Data Stale, Spike Override Active)

### 2. First optimization cycle ran

Check the **Battery Plan** sensor. Within 5 minutes of setup it should show:
- A numeric state (target SoC percentage)
- Attributes including `mode`, `reason`, `target_register`

If the state is `unknown` or `unavailable`, check the HA logs (Settings > System > Logs) and filter for `victron_mpc`.

### 3. Shadow mode is ON

Verify that the **Shadow Mode** switch is ON (the default). This means the integration is computing decisions but NOT writing Modbus registers. You should see log messages like:

```
SHADOW: Would write R2901=450, R2706=0
```

### 4. Decision sensor has context

The **Decision** sensor should show attributes including:
- `buy_price_actual` -- current Amber price
- `battery_soc_pct` -- current battery level
- `solar_day_type` -- weather classification
- `solar_forecast_source` -- which forecast method is active

## Shadow Mode

Shadow mode is the integration's safe startup mode. When enabled:

- The full optimization cycle runs every 5 minutes
- All sensor entities update with real decisions
- **No Modbus registers are written** -- your Victron system is unaffected
- Log messages show what would have been written

This lets you:
- Verify the integration is making sensible decisions
- Compare MPC recommendations against your current setup
- Build confidence before going live

**To go live**: Turn off the Shadow Mode switch when you are satisfied with the decisions. The integration will immediately begin writing R2901 and R2706 registers to your Cerbo GX.

**To return to shadow mode**: Turn the switch back on at any time. The integration will stop writing registers but continue computing decisions.
