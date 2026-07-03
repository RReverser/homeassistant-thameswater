"""Thames Water tariff scraping.

Thames Water does not expose tariffs through the account API used elsewhere in
this integration: metered charges are a fixed annual "Scheme of Charges",
published per region (identical for every customer) rather than per account.

The current figures are, however, rendered in the static HTML of Thames Water's
public "metered customers" help page, so we scrape them there. They only change
once a year (1 April), so a daily refresh is more than enough, and because the
data is region-wide it needs no authentication.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import urllib.request

TARIFF_URL = (
    "https://www.thameswater.co.uk/help/account-and-billing/"
    "understand-your-bill/metered-customers"
)

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)

# One cubic metre of water is 1000 litres.
_LITRES_PER_M3 = 1000
_DAYS_PER_YEAR = 365


class TariffError(Exception):
    """Raised when the tariff page cannot be fetched or parsed."""


@dataclass(frozen=True)
class Tariff:
    """Metered household tariff for the Thames Water region."""

    clean_water_rate_per_m3: float
    wastewater_rate_per_m3: float
    water_fixed_per_year: float
    wastewater_fixed_per_year: float

    @property
    def volumetric_rate_per_m3(self) -> float:
        """Combined clean water + wastewater volumetric rate (GBP/m3)."""
        return round(self.clean_water_rate_per_m3 + self.wastewater_rate_per_m3, 4)

    @property
    def unit_rate_per_litre(self) -> float:
        """Combined volumetric rate expressed per litre (GBP/L).

        This matches the litre-denominated consumption statistics, so it can be
        attached directly to the Energy dashboard water source as a price entity.
        """
        return (
            self.clean_water_rate_per_m3 + self.wastewater_rate_per_m3
        ) / _LITRES_PER_M3

    @property
    def standing_charge_per_day(self) -> float:
        """Combined fixed/standing charge expressed per day (GBP/day)."""
        return round(
            (self.water_fixed_per_year + self.wastewater_fixed_per_year)
            / _DAYS_PER_YEAR,
            4,
        )


def _search_float(pattern: str, text: str, description: str) -> float:
    """Return the first captured group of ``pattern`` in ``text`` as a float."""
    match = re.search(pattern, text)
    if match is None:
        raise TariffError(
            f"Could not find {description} on the Thames Water tariff page "
            "(the page markup may have changed)"
        )
    return float(match.group(1))


def parse_tariff(html: str) -> Tariff:
    """Parse the metered-customers help page HTML into a :class:`Tariff`.

    The figures live inside markup (``<strong>`` tags and a table) and, on the
    server-rendered React payload, are separated from their labels by escaped
    JSON. Stripping tags and collapsing whitespace leaves each value adjacent to
    its label, which the regexes below anchor on.
    """
    text = re.sub(r"<[^>]+>", " ", html).replace('\\"', '"')
    text = re.sub(r"\s+", " ", text)

    return Tariff(
        clean_water_rate_per_m3=_search_float(
            r"£([0-9]+\.[0-9]+) per m3 for clean water",
            text,
            "the clean water volumetric rate",
        ),
        wastewater_rate_per_m3=_search_float(
            r"£([0-9]+\.[0-9]+) per m3 for wastewater",
            text,
            "the wastewater volumetric rate",
        ),
        water_fixed_per_year=_search_float(
            r"Water £([0-9]+\.[0-9]+) Not applicable",
            text,
            "the water fixed charge",
        ),
        # The wastewater row lists the standard fixed charge first and the
        # (lower) surface-water-drainage rebate charge second; take the standard.
        wastewater_fixed_per_year=_search_float(
            r"Wastewater £([0-9]+\.[0-9]+) £",
            text,
            "the wastewater fixed charge",
        ),
    )


def fetch_tariff(url: str = TARIFF_URL) -> Tariff:
    """Fetch and parse the current tariff. Blocking; run in an executor."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            html = response.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, TimeoutError) as err:
        raise TariffError(f"Failed to fetch the Thames Water tariff page: {err}") from err
    return parse_tariff(html)
