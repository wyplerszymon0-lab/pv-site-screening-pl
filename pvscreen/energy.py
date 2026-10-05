"""Energy yield of candidate areas from NASA POWER satellite climatology.

A Python port of the yield model validated against PVGIS in Solar Site
Intelligence (solar-model.js): monthly NASA POWER means of global and diffuse
horizontal irradiance and air temperature, transposed to the tilted plane with
the isotropic-sky model (Liu & Jordan; Duffie & Beckman, "Solar Engineering of
Thermal Processes", §2.19), with ASHRAE glass reflection losses, temperature
derating and system losses. The tests check it against the JavaScript model and
against PVGIS.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import requests

from pvscreen.boundary import USER_AGENT

DEG = math.pi / 180
# Klein's recommended average day of each month (Duffie & Beckman, Table 1.6.1).
MEAN_DAY = (17, 47, 75, 105, 135, 162, 198, 228, 258, 288, 318, 344)
DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
MONTH_KEYS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
HOUR_ANGLES = np.arange(-180, 180, 0.5) * DEG  # the same 0.5° steps as the JavaScript model

DEFAULT_CACHE = Path("data/nasa_power")
POWER_URL = "https://power.larc.nasa.gov/api/temporal/climatology/point"


@dataclass(frozen=True)
class ModelOptions:
    system_loss: float = 0.14  # wiring, inverter, soiling, mismatch; PVGIS's default input
    temp_coeff: float = -0.004  # power change per °C of cell temperature, crystalline silicon
    cell_temp_rise: float = 20.0  # cell above ambient while producing, °C
    ground_albedo: float = 0.2  # standard ground reflectance
    iam_b0: float = 0.05  # ASHRAE incidence-angle modifier for glass; 0 disables it


@dataclass(frozen=True)
class Climatology:
    ghi: tuple[float, ...]  # kWh/m²/day, monthly means
    diffuse: tuple[float, ...]
    temp: tuple[float, ...]  # °C


def declination(n: int) -> float:
    return 23.45 * math.sin(2 * math.pi * (284 + n) / 365)


def incidence_modifier(cos_theta, b0: float):
    """Share of light passing the module glass (ASHRAE): 1 - b0 (1/cos θ - 1)."""
    cos_theta = np.asarray(cos_theta, dtype=float)
    with np.errstate(divide="ignore"):
        k = np.maximum(0.0, 1 - b0 * (1 / cos_theta - 1))
    return np.where(cos_theta > 0, k, 0.0)


def sky_incidence_deg(tilt: float) -> float:
    return 59.7 - 0.1388 * tilt + 0.001497 * tilt * tilt


def ground_incidence_deg(tilt: float) -> float:
    return 90 - 0.5788 * tilt + 0.002693 * tilt * tilt


def beam_tilt_factor(lat: float, tilt: float, compass_az: float, n: int, b0: float = 0.0) -> float:
    """Daily beam irradiation on the tilted plane over that on the horizontal.

    compass_az: 0 = north, 90 = east, 180 = south.
    """
    phi, beta = lat * DEG, tilt * DEG
    gamma = (compass_az - 180) * DEG  # from south, west positive
    d = declination(n) * DEG
    w = HOUR_ANGLES
    cos_z = math.cos(phi) * math.cos(d) * np.cos(w) + math.sin(phi) * math.sin(d)
    cos_t = (
        math.sin(d) * math.sin(phi) * math.cos(beta)
        - math.sin(d) * math.cos(phi) * math.sin(beta) * math.cos(gamma)
        + math.cos(d) * math.cos(phi) * math.cos(beta) * np.cos(w)
        + math.cos(d) * math.sin(phi) * math.sin(beta) * math.cos(gamma) * np.cos(w)
        + math.cos(d) * math.sin(beta) * math.sin(gamma) * np.sin(w)
    )
    up = cos_z > 0  # sun above the horizon
    on_plane = np.maximum(0.0, cos_t[up])
    if b0 > 0:
        on_plane = on_plane * incidence_modifier(cos_t[up], b0)
    horizontal = cos_z[up].sum()
    return float(on_plane.sum() / horizontal) if horizontal > 0 else 0.0


def monthly_yield(
    clim: Climatology, lat: float, tilt: float, compass_az: float, opts: ModelOptions | None = None
):
    """Monthly and annual yield in kWh per kWp, and annual plane-of-array irradiation."""
    opts = opts or ModelOptions()
    cos_b = math.cos(tilt * DEG)
    k_sky = (
        float(incidence_modifier(math.cos(sky_incidence_deg(tilt) * DEG), opts.iam_b0))
        if opts.iam_b0
        else 1.0
    )
    k_gnd = (
        float(incidence_modifier(math.cos(ground_incidence_deg(tilt) * DEG), opts.iam_b0))
        if opts.iam_b0
        else 1.0
    )
    monthly, poa_total = [], 0.0
    for m, n in enumerate(MEAN_DAY):
        h = clim.ghi[m]
        hd = min(clim.diffuse[m], h)
        rb = beam_tilt_factor(lat, tilt, compass_az, n, opts.iam_b0)
        poa_daily = (
            (h - hd) * rb + hd * (1 + cos_b) / 2 * k_sky + h * opts.ground_albedo * (1 - cos_b) / 2 * k_gnd
        )
        temp_factor = 1 + opts.temp_coeff * (clim.temp[m] + opts.cell_temp_rise - 25)
        poa = poa_daily * DAYS_IN_MONTH[m]
        poa_total += poa
        monthly.append(poa * (1 - opts.system_loss) * temp_factor)
    return {"monthly": monthly, "annual": sum(monthly), "poa_annual": poa_total}


def optimal_tilt(clim: Climatology, lat: float, max_tilt: int = 75, opts: ModelOptions | None = None):
    """Whole-degree tilt (equator-facing) with the highest annual yield, and that yield."""
    opts = opts or ModelOptions()
    az = 180 if lat >= 0 else 0
    curve = [monthly_yield(clim, lat, t, az, opts)["annual"] for t in range(max_tilt + 1)]
    best = int(np.argmax(curve))
    return best, curve[best]


def parse_power_climatology(payload: dict) -> Climatology:
    p = payload.get("properties", {}).get("parameter", {})

    def series(name):
        values = [p.get(name, {}).get(k) for k in MONTH_KEYS]
        if not all(isinstance(v, int | float) and v > -900 for v in values):
            raise ValueError("NASA POWER returned incomplete climatology for this location")
        return tuple(float(v) for v in values)

    return Climatology(series("ALLSKY_SFC_SW_DWN"), series("ALLSKY_SFC_SW_DIFF"), series("T2M"))


def power_params(lat: float, lon: float) -> dict[str, str]:
    return {
        "parameters": "ALLSKY_SFC_SW_DWN,ALLSKY_SFC_SW_DIFF,T2M",
        "community": "RE",
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "format": "JSON",
    }


def fetch_climatology(
    lat: float, lon: float, cache_dir: Path = DEFAULT_CACHE, session: requests.Session | None = None
) -> Climatology:
    """Climatology at a point, cached per 0.1° cell (NASA POWER's own grid is coarser)."""
    lat, lon = round(lat, 1), round(lon, 1)
    path = Path(cache_dir) / f"power_{lat:.1f}_{lon:.1f}.json"
    if not path.exists():
        resp = (session or requests.Session()).get(
            POWER_URL, params=power_params(lat, lon), headers={"User-Agent": USER_AGENT}, timeout=120
        )
        resp.raise_for_status()
        parse_power_climatology(resp.json())  # validate before caching
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(resp.text, encoding="utf-8")
    return parse_power_climatology(json.loads(path.read_text(encoding="utf-8")))


def add_energy(
    candidates: gpd.GeoDataFrame,
    mwp_per_ha: float,
    system_loss: float = ModelOptions.system_loss,
    cache_dir: Path = DEFAULT_CACHE,
    session: requests.Session | None = None,
) -> gpd.GeoDataFrame:
    """Add tilt, yield per kWp, installed capacity and annual energy to each candidate.

    Panels are assumed fixed, facing the equator, at the tilt that maximises the
    annual yield in the candidate's climate.
    """
    opts = ModelOptions(system_loss=system_loss)
    out = candidates.copy()
    centroids = out.geometry.centroid.to_crs(4326)
    tilts, yields = [], []
    for pt in centroids:
        clim = fetch_climatology(pt.y, pt.x, cache_dir, session)
        tilt, kwh = optimal_tilt(clim, pt.y, opts=opts)
        tilts.append(tilt)
        yields.append(kwh)
    out["tilt_deg"] = tilts
    out["yield_kwh_per_kwp"] = yields
    out["capacity_mwp"] = out["area_ha"] * mwp_per_ha
    out["energy_mwh_year"] = out["capacity_mwp"] * out["yield_kwh_per_kwp"]  # MWp x kWh/kWp = MWh
    return out
