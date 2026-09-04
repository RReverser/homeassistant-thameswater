# Thames Water Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)

# Home Assistant Integration for Thames Water Consumption Data

This Home Assistant integration retrieves water consumption data from Thames Water using their API. It allows you to monitor your water usage directly from your Home Assistant setup without needing additional devices.

You need a Thames Water Smart Meter. The water consumption data provided by this integration is delayed by approximately three days or more. This delay is a characteristic of the Thames Water data system and cannot be altered in this integration.

It uses the [thameswaterapi](https://github.com/jelmer/thameswaterapi) Python package to interact with the Thames Water API.

## Sensors

The integration exposes the following entities:

* **Water consumption** — the latest meter read in litres, with hourly external statistics injected for use in the Energy dashboard.
* **Outstanding balance** — the amount currently due on your Thames Water account, in GBP. The current balance and an `is_in_credit` flag are exposed as attributes.
* **Tariff** — the current metered-household charges for the Thames Water region:
  * **Unit Rate** (`GBP/L`) — combined clean water + wastewater volumetric rate, per litre. Because it is denominated in litres it can be attached directly to the Energy dashboard water source as the price entity.
  * **Standing Charge** (`GBP/day`) — combined water + wastewater fixed charge per day.
  * **Volumetric Rate** (`GBP/m³`) — the combined rate per cubic metre.
  * Individual **Clean Water Rate**, **Wastewater Rate**, **Water Fixed Charge** and **Wastewater Fixed Charge** sensors (disabled by default; enable them for a full bill breakdown).

  Thames Water has no tariff API — metered charges are a fixed annual "Scheme of Charges", published per region rather than per account, so the same figures apply to every customer. This integration reads them from Thames Water's public [metered customers](https://www.thameswater.co.uk/help/account-and-billing/understand-your-bill/metered-customers) help page (no credentials required) and refreshes daily; they normally only change on 1 April. The fixed charges use the standard rate (not the surface-water-drainage rebate rate). If the page layout ever changes the tariff sensors become unavailable but consumption and balance are unaffected.

### Water cost in the Energy dashboard

Attach the **Unit Rate** sensor as the price for your water source: in **Settings → Dashboards → Energy → Water consumption**, edit the source and choose *Use an entity tracking the total costs*/*Use a price entity* and select `sensor.thames_water_unit_rate`. (The standing charge is a flat daily amount and is not part of the volumetric price.)

## Installation

### Installation through HACS

1. Install the custom component using the Home Assistant Community Store (HACS) by adding the Custom Repository:
https://github.com/jelmer/HA-Thames-Water
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

## Energy Management

The water statistics can be integrated into HA [Home Energy Management](https://www.home-assistant.io/docs/energy/) using **thames_water:thameswater_consumption**.

Data is fetched every 12 hours by default, in a single hourly request covering
everything missing since the last one, so an instance that was switched off for
a while catches up in one go rather than losing those days.

Setup asks how many days of past readings to import, defaulting to the 30 the
meters page itself shows. The whole range arrives in one request whatever its
width, so a deeper import costs no more than a shallow one. The import happens
before the entry is created, so a meter that answers with nothing or a login
that has expired shows up in the dialog rather than in the log an hour later.
Refreshes from then on resume from the newest reading stored, so importing
more later means setting the integration up again.

Readings run about three days behind. A response ending today is truncated at a
whole-day boundary rather than padded, so the days not yet published are simply
absent and the next refresh asks for them again.

[![Open your Home Assistant instance and show your Energy configuration panel.](https://my.home-assistant.io/badges/config_energy.svg)](https://my.home-assistant.io/redirect/config_energy/)

![Dashboard](./dashboard.png)

## Acknowledgements

This integration is based on the original work by [Ayrton Bourn (AyrtonB)](https://github.com/AyrtonB).
