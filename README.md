# Thames Water Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)

# Home Assistant Integration for Thames Water Consumption Data

This Home Assistant integration retrieves water consumption data from Thames Water using their API. It allows you to monitor your water usage directly from your Home Assistant setup without needing additional devices.

You need a Thames Water Smart Meter. The water consumption data provided by this integration is delayed by approximately three days or more. This delay is a characteristic of the Thames Water data system and cannot be altered in this integration.

It uses the [thameswaterapi](https://github.com/RReverser/thameswaterapi) Python package to interact with the Thames Water API.

## Setup

Sign in with your Thames Water email and password. That is the whole of it:
your contract accounts and the meters on them are discovered on every refresh,
so a meter added to your account later appears by itself.

Data is fetched every 12 hours, in a single request covering everything missing
since the last one, so an instance that was switched off for a while catches up
in one go. To poll on your own schedule, turn off **Enable polling for updates**
in the entry's system options and call `homeassistant.update_entity` from an
automation, targeting one of the meter reading sensors. Manual refreshes are
debounced to one per 10 seconds.

## Entities

One device per contract account, named by its property address, and one device
per meter beneath it.

* **Meter reading** — the newest reading Thames Water has published for that
  meter, in litres. It carries no `state_class`: the reading is about three days
  old, and Home Assistant would record it against the time it was fetched. The
  history is in the statistics below, timestamped correctly.
* **Last reading** — when that reading was taken (diagnostic).
* **Outstanding balance** — the amount currently due on that contract account,
  in GBP, with the current balance and an `is_in_credit` flag as attributes.
* **Tariff** — the current metered-household charges for the Thames Water
  region, on their own device:
  * **Unit rate** (`GBP/L`) — combined clean water + wastewater volumetric rate.
  * **Standing charge** (`GBP/day`) — combined fixed charge per day.
  * **Volumetric rate** (`GBP/m³`) — the combined rate per cubic metre.
  * Individual **Clean water rate**, **Wastewater rate**, **Water fixed charge**
    and **Wastewater fixed charge** sensors (disabled by default; enable them
    for a full bill breakdown).

  Thames Water has no tariff API — metered charges are a fixed annual "Scheme of
  Charges", published per region rather than per account, so the same figures
  apply to every customer. This integration reads them from Thames Water's public
  [metered customers](https://www.thameswater.co.uk/help/account-and-billing/understand-your-bill/metered-customers)
  help page (no credentials required) and refreshes weekly; they normally only
  change on 1 April. That page is a customer-facing summary rather than the
  Scheme of Charges itself, so it may not reflect banding or variation by area,
  and the fixed charges it lists are the standard rate rather than the
  surface-water-drainage rebate rate. If the page changes shape the sensors keep
  reporting the figures last read, and consumption and balance are unaffected.

## Statistics

Two long-term statistics per meter, which is where the history lives:

* `thames_water:{meter}_consumption` — litres, the meter's own odometer reading.
  Because the value is the odometer rather than a running total this integration
  computes, re-importing a day writes the same numbers again, and a reading
  missed once is covered by the next one.
* `thames_water:{meter}_cost` — GBP.

Readings marked as estimated by Thames Water are written like any other: they
are the utility's own figures, and the odometer is what matters.

### Water in the Energy dashboard

In **Settings → Dashboards → Energy → Water consumption**, add
`thames_water:{meter}_consumption` as the source and choose *Use a statistic
tracking the total costs*, then pick `thames_water:{meter}_cost`.

**Do not attach a price entity instead.** The Energy dashboard builds its own
cost sensor only for a source whose statistic is an entity ID, and this
integration writes an external statistic, whose colon fails that check. A price
entity attached to it is ignored silently — no error and no log line. The
**Unit rate** sensor is a figure to read, then, rather than something to attach;
it is also denominated per litre where a price entity is read as a price per
cubic metre.

Two things to know about the cost figures:

* Each reading is priced at the rate in force **on its own date**, using the
  effective date the tariff page states. Readings arrive around three days late,
  so without that a window spanning a price change would take whichever rate
  happened to be scraped that run.
* Only the current rate is published, so readings from **before** that rate took
  effect are left unpriced rather than valued at a rate that was not theirs.
  Correcting a rate later does not reprice readings already written.

The standing charge is a flat daily amount, not part of a volumetric price, and
is not included in the cost statistic.

## Importing older readings

A new entry imports the last 30 days. To go further back, call the
**Import history** action against a meter reading sensor with a start date:

```yaml
action: thames_water.import_history
target:
  entity_id: sensor.meter_311228415_meter_reading
data:
  start_date: "2026-02-01"
```

The whole range is one request, whatever its width, and re-running it is safe.
Pick your move-in date rather than the earliest date with data: readings exist
from before you moved in, so a deeper start imports the previous occupant's
usage.

## Upgrading from 1.2.x

This release changes the shape of everything a 1.2.x setup pointed at:

* Entity IDs change. Entities are now named per device — a meter or a contract
  account — rather than carrying "Thames Water" in every name.
* The old statistics (`thames_water:thameswater_consumption`, and its `_hourly`
  and `_daily` variants) stop being written and are replaced by one
  `thames_water:{meter}_consumption` series per meter. Repoint the Energy
  dashboard at the new one. Nothing is deleted: Developer Tools → Statistics
  lists the old series as no longer provided, with a removal action, whenever
  you want them gone.
* The **Meter reading** sensor loses its `state_class`, so Home Assistant raises
  a repair issue about the statistics it used to generate. That is the removal
  being noticed, not a fault; the same action clears it.
* The polling interval is no longer a setup question. See **Setup** above.

## Installation

### Installation through HACS

1. Install the custom component using the Home Assistant Community Store (HACS) by adding the Custom Repository:
https://github.com/RReverser/homeassistant-thameswater
2. In the HACS panel, select Thames Water from the repository list and select the DOWNLOAD button.
3. Restart HA
4. Go to Settings > Devices & Services > Add Integration and select Thames Water.

### Manual installation

Copy the `custom_components/thames_water/` directory and all of its files to your `config/custom_components/` directory.

## Configuration

Once installed, restart Home Assistant:

[![Open your Home Assistant instance and show the system dashboard.](https://my.home-assistant.io/badges/system_dashboard.svg)](https://my.home-assistant.io/redirect/system_dashboard/)

Then, add the integration:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=thames_water)


<details>
  <summary>Manually add the Integration</summary>
  Visit the <i>Integrations</i> section in Home Assistant and click the <i>Add</i> button in the bottom right corner. Search for <code>Thames Water</code> and input your credentials. <b>You may need to clear your browser cache before the integration appears in the list.</b>
</details>

## Acknowledgements

This integration is based on the original work by [Ayrton Bourn (AyrtonB)](https://github.com/AyrtonB).
