"""
ship_diversion_checker.py

Cron job:
  1. Reads all ship rows from Google Sheets via gsheet_handler.
  2. Fetches the latest positions from Kpler via kpler_handler.
  3. Merges new positions into Coord_Trace and writes back to the sheet.
  4. Runs multi-signal diversion detection on each ship.
  5. Sends Telegram alerts for any flagged ships.

Install deps:
    pip install gspread google-auth numpy requests httpx python-dotenv

Cron example (every 30 min):
    */30 * * * * /usr/bin/python3 /path/to/ship_diversion_checker.py >> /var/log/ship_checker.log 2>&1

Sheet columns (must match COLUMNS in gsheet_handler.py):
    Last_Updated | IMO | Name | KPLER_ID | Departure |
    Coord_Trace | Original_Dest | Original_Dest_Lat | Original_Dest_Long
"""

import json
import math
import logging
import os
import requests
import numpy as np
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

from kpler_handler import KplerSession
from gsheet_handler import GSheet_Handler, get_all_ships, upsert_ship

load_dotenv('secrets.env')

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION  —  edit these before running
# ══════════════════════════════════════════════════════════════════════════════

# Telegram  (leave as-is to run in stub/log-only mode)
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_KEY", "YOUR_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "YOUR_CHANNEL_ID")

# Detection tuning
MIN_PINGS_REQUIRED        = 5    # skip ships with fewer pings than this
N_RECENT_PINGS            = 100    # how many most-recent pings to analyse
DIVERSION_SCORE_THRESHOLD = 2    # flag if this many signals (out of 3) fire

# AIS gap handling
AIS_GAP_THRESHOLD_HOURS   = 24    # consecutive ping gap (hours) that counts as AIS-off event
AIS_JUMP_SG_APPROACH_NM   = 100  # if ship reappears >= this much closer to SG after a gap, flag as suspicious jump

# Singapore reference point
SINGAPORE = {"lat": 1.246849819, "lon": 103.6395263672} # Straits Area

# If the stated destination is within this radius of Singapore, the ship is
# legitimately Singapore-bound — skip diversion detection entirely.
SINGAPORE_DEST_RADIUS_NM = 50

# Known chokepoints — diversion flags are SUPPRESSED when a ship is inside
# one of these radii, since course changes there are expected and normal.
CHOKEPOINTS = [
    {"name": "Strait of Malacca",  "lat":  2.5,  "lon": 102.0, "radius_nm": 120},
    {"name": "Strait of Lombok",   "lat": -8.7,  "lon": 115.7, "radius_nm":  60},
    {"name": "Sunda Strait",       "lat": -6.0,  "lon": 105.9, "radius_nm":  60},
    {"name": "Cape of Good Hope",  "lat": -34.4, "lon":  18.5, "radius_nm": 200},
    {"name": "Cape Horn",          "lat": -55.9, "lon": -67.3, "radius_nm": 200},
    {"name": "Suez Canal",         "lat":  30.5, "lon":  32.3, "radius_nm": 100},
    {"name": "Bab-el-Mandeb",      "lat":  12.6, "lon":  43.4, "radius_nm": 100},
    {"name": "Strait of Hormuz",   "lat":  26.6, "lon":  56.3, "radius_nm":  80},
]

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# GEO UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

EARTH_RADIUS_NM = 3440.065  # nautical miles


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(math.sqrt(a))


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing (°, 0–360) from point 1 toward point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def bearing_diff(b1: float, b2: float) -> float:
    """Absolute angular difference between two bearings (0–180°)."""
    diff = abs(b1 - b2) % 360
    return min(diff, 360 - diff)


def cross_track_distance_nm(lat_p: float, lon_p: float,
                            lat1: float, lon1: float,
                            lat2: float, lon2: float) -> float:
    """
    Signed cross-track distance (nm) of point P from the great-circle A→B.
    Positive = right of track, negative = left of track.
    Returns 0.0 if A == B (degenerate route).
    """
    if haversine_nm(lat1, lon1, lat2, lon2) < 0.1:
        return 0.0
    d_ap = haversine_nm(lat1, lon1, lat_p, lon_p) / EARTH_RADIUS_NM   # angular dist, radians
    theta_ap = math.radians(initial_bearing(lat1, lon1, lat_p, lon_p))
    theta_ab = math.radians(initial_bearing(lat1, lon1, lat2, lon2))
    xtd = math.asin(math.sin(d_ap) * math.sin(theta_ap - theta_ab))   # radians
    return xtd * EARTH_RADIUS_NM


def is_near_chokepoint(lat: float, lon: float) -> Optional[str]:
    """Returns the name of the chokepoint if the position is inside one, else None."""
    for cp in CHOKEPOINTS:
        if haversine_nm(lat, lon, cp['lat'], cp['lon']) <= cp["radius_nm"]:
            return cp["name"]
    return None


# ══════════════════════════════════════════════════════════════════════════════
# AIS GAP DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def find_last_gap(sorted_pings: list) -> Optional[int]:
    """
    Scan sorted_pings (ascending timestamp) for the last gap that exceeds
    AIS_GAP_THRESHOLD_HOURS.  Returns the index of the FIRST ping AFTER the
    gap, or None if no significant gap exists.

    Example: pings = [p0, p1, p2, GAP, p3, p4]
             returns 3  (p3 is the first ping after the gap)
    """
    threshold_s = AIS_GAP_THRESHOLD_HOURS * 3600
    last_gap_idx = None
    for i in range(1, len(sorted_pings)):
        # 1. Get the raw values
        raw_prev = sorted_pings[i - 1].get("receivedTime", 0)
        raw_curr = sorted_pings[i].get("receivedTime", 0)

        # 2. Convert strings to datetime objects if they aren't already
        # (Assuming '0' is your fallback, we handle that too)
        t_prev = datetime.fromisoformat(raw_prev) if isinstance(raw_prev, str) else raw_prev
        t_curr = datetime.fromisoformat(raw_curr) if isinstance(raw_curr, str) else raw_curr

        # 3. Calculate the gap in seconds
        # Subtracting two datetimes creates a timedelta object
        if isinstance(t_curr, datetime) and isinstance(t_prev, datetime):
            gap_seconds = (t_curr - t_prev).total_seconds()
            
            if gap_seconds >= threshold_s:
                    last_gap_idx = i
    return last_gap_idx


def check_ais_jump(sorted_pings: list, gap_idx: int, sg_bound: bool) -> dict:
    """
    Given the index of the first post-gap ping, compute jump metadata and
    determine whether the jump is suspicious — direction depends on mode:

      sg_bound=False (non-SG-bound): suspicious if ship reappears significantly
                                     CLOSER to Singapore (unexpected approach).
      sg_bound=True  (SG-bound):     suspicious if ship reappears significantly
                                     FARTHER from Singapore (unexpected departure,
                                     e.g. Al Zour → SG ship jumps to South Africa).

    sg_delta_nm = sg_dist_pre - sg_dist_post
      positive → ship is now closer to SG
      negative → ship is now farther from SG
    """
    pre  = sorted_pings[gap_idx - 1]
    post = sorted_pings[gap_idx]

    gap_hours    = (datetime.fromisoformat(post.get("receivedTime", 0)) - datetime.fromisoformat(pre.get("receivedTime", 0))).total_seconds() / 3600
    jump_nm      = haversine_nm(pre["lat"], pre["lon"], post["lat"], post["lon"])

    sg_dist_pre  = haversine_nm(pre["lat"],  pre["lon"],  SINGAPORE["lat"], SINGAPORE["lon"])
    sg_dist_post = haversine_nm(post["lat"], post["lon"], SINGAPORE["lat"], SINGAPORE["lon"])
    sg_delta_nm  = sg_dist_pre - sg_dist_post   # positive = closer, negative = farther

    if sg_bound:
        # Suspicious when ship moved significantly AWAY from SG
        suspicious = (-sg_delta_nm) >= AIS_JUMP_SG_APPROACH_NM
        direction  = "away from SG"
    else:
        # Suspicious when ship moved significantly TOWARD SG
        suspicious = sg_delta_nm >= AIS_JUMP_SG_APPROACH_NM
        direction  = "toward SG"

    return {
        "gap_hours":          round(gap_hours, 1),
        "jump_nm":            round(jump_nm, 1),
        "sg_dist_before_nm":  round(sg_dist_pre, 1),
        "sg_dist_after_nm":   round(sg_dist_post, 1),
        "sg_delta_nm":        round(sg_delta_nm, 1),   # + = closer, - = farther
        "suspicious_jump":    suspicious,
        "suspicious_direction": direction,
    }


# ══════════════════════════════════════════════════════════════════════════════
# DIVERSION DETECTION
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class DiversionResult:
    ship_id:          str
    ship_name:        str
    score:            int                      # 0–3 from the 3-signal algo
    flagged:          bool
    signals:          dict = field(default_factory=dict)
    near_chokepoint:  Optional[str] = None
    latest_ping:      Optional[dict] = None
    reason:           str = ""
    ais_gap_detected: bool = False             # True if a gap >= threshold found
    ais_jump_info:    Optional[dict] = None    # populated when gap detected
    ais_jump_flagged: bool = False             # True if jump itself is suspicious


def detect_diversion(
    ship_name: str,
    ship_id:   str,
    dest_lat:  float,
    dest_lon:  float,
    origin_lat: float,
    origin_lon: float,
    pings:     list,
) -> DiversionResult:
    """
    Score a ship on three independent signals.  Returns a DiversionResult.

    Signal 1 — Singapore approach rate
        Fit a weighted linear regression on distance-to-SG over the recent
        pings.  A consistently negative slope means the ship is closing in
        on Singapore.

    Signal 2 — Bearing alignment toward Singapore
        For each recent ping compare the ship's reported course to the
        bearing from that position toward Singapore.  If ≥60 % of pings
        are within 45° of that bearing the signal fires.

    Signal 3 — Cross-track drift toward Singapore
        Compute signed cross-track distance of each recent ping from the
        great-circle origin→destination.  If the majority of pings are on
        Singapore's side of that route AND the deviation magnitude is
        growing, the signal fires.

    Chokepoint suppression:
        If the latest position is inside a known chokepoint the flag is
        suppressed (score still recorded for audit purposes).
    """

    def skip(reason: str) -> DiversionResult:
        return DiversionResult(ship_id=ship_id, ship_name=ship_name,
                               score=0, flagged=False, reason=reason)

    if len(pings) < MIN_PINGS_REQUIRED:
        return skip(f"Insufficient pings ({len(pings)} < {MIN_PINGS_REQUIRED} required)")

    # Sort ascending by timestamp; take the last N
    sorted_pings = sorted(pings, key=lambda p: p.get("receivedTime", 0))
    recent_all   = sorted_pings[-N_RECENT_PINGS:]
    latest       = recent_all[-1]

    # ── Determine detection mode early (needed by gap check) ──────────────
    dist_dest_to_sg = haversine_nm(dest_lat, dest_lon,
                                   SINGAPORE["lat"], SINGAPORE["lon"])
    sg_bound = dist_dest_to_sg <= SINGAPORE_DEST_RADIUS_NM
    mode = "sg_bound" if sg_bound else "non_sg_bound"

    # ── AIS gap detection ─────────────────────────────────────────────────
    # Find the last significant gap within the recent window.
    gap_idx      = find_last_gap(recent_all)
    ais_gap      = gap_idx is not None
    jump_info    = None
    jump_flagged = False

    if ais_gap:
        jump_info    = check_ais_jump(recent_all, gap_idx, sg_bound)
        jump_flagged = jump_info["suspicious_jump"]
        post_gap_pings = recent_all[gap_idx:]
        log.info(
            "  AIS gap detected: %.1f hrs dark, jumped %.1f nm, "
            "SG delta: %.1f nm %s (suspicious=%s)",
            jump_info["gap_hours"], jump_info["jump_nm"],
            abs(jump_info["sg_delta_nm"]), jump_info["suspicious_direction"],
            jump_flagged,
        )
        if len(post_gap_pings) < MIN_PINGS_REQUIRED:
            # Not enough post-gap pings to run 3-signal algo reliably.
            # Surface whatever we know from the jump itself.
            if sg_bound:
                jump_desc = (
                    f"ship moved {abs(jump_info['sg_delta_nm'])} nm "
                    f"away from SG after going dark"
                )
            else:
                jump_desc = (
                    f"ship appeared {abs(jump_info['sg_delta_nm'])} nm "
                    f"closer to SG after going dark"
                )
            reason = (
                f"AIS gap of {jump_info['gap_hours']}h detected; "
                f"only {len(post_gap_pings)} post-gap ping(s) — "
                f"insufficient for full signal analysis"
            )
            if jump_flagged:
                reason += f"; suspicious jump: {jump_desc}"
            return DiversionResult(
                ship_id=ship_id, ship_name=ship_name,
                score=0, flagged=jump_flagged,
                reason=reason,
                latest_ping=latest,
                ais_gap_detected=True,
                ais_jump_info=jump_info,
                ais_jump_flagged=jump_flagged,
            )
        # Enough post-gap pings — run signals only on those
        recent = post_gap_pings
    else:
        recent = recent_all

    signals: dict = {}
    signals["mode"] = mode
    signals["ais_gap_detected"] = ais_gap
    if jump_info:
        signals["ais_jump"] = jump_info
    chokepoint = is_near_chokepoint(latest['geo']["lat"], latest["geo"]["lon"])
    signals["mode"] = mode

    # ── Shared regression setup ────────────────────────────────────────────
    timestamps = [datetime.fromisoformat(p["receivedTime"]) for p in recent]
    t0         = timestamps[0]
    norm_t     = [(t - t0).total_seconds() for t in timestamps]
    weights    = [float(i + 1) for i in range(len(recent))]   # recency weights

    sg_dists = [
        haversine_nm(p['geo']['lat'], p['geo']['lon'], SINGAPORE["lat"], SINGAPORE["lon"])
        for p in recent
    ]
    dest_dists = [
        haversine_nm(p['geo']['lat'], p['geo']['lon'], dest_lat, dest_lon)
        for p in recent
    ]

    sg_slope = float(
        np.polyfit(norm_t, sg_dists, 1, w=weights)[0]
    ) if max(norm_t) > 0 else 0.0

    dest_slope = float(
        np.polyfit(norm_t, dest_dists, 1, w=weights)[0]
    ) if max(norm_t) > 0 else 0.0

    signals["sg_slope"]           = round(sg_slope, 5)
    signals["dest_slope"]         = round(dest_slope, 5)
    signals["sg_distances_nm"]    = [round(d, 1) for d in sg_dists]
    signals["dest_distances_nm"]  = [round(d, 1) for d in dest_dists]

    threshold_pings = math.ceil(len(recent) * 0.6)

    # ══════════════════════════════════════════════════════════════════════
    # MODE A — NON-SG-BOUND: detect unexpected approach toward Singapore
    # ══════════════════════════════════════════════════════════════════════
    if not sg_bound:

        # Signal 1: Closing on SG while NOT making progress toward destination
        #   Both must be true simultaneously to avoid flagging ships that
        #   naturally pass near Singapore en-route to nearby ports.
        sg_approach  = sg_slope  < -0.001   # genuinely closing on SG
        dest_leaving = dest_slope > -0.005  # not meaningfully approaching dest
        signal_1     = sg_approach and dest_leaving

        signals["sg_approach"]  = sg_approach
        signals["dest_leaving"] = dest_leaving

        # Signal 2: Course bearing persistently aligned toward Singapore
        bearing_matches = 0
        for p in recent:
            bearing_to_sg = initial_bearing(p["geo"]["lat"], p["geo"]["lon"],
                                            SINGAPORE["lat"], SINGAPORE["lon"])
            ship_course   = p.get("course") or p.get("heading")
            if ship_course is not None:
                if bearing_diff(bearing_to_sg, float(ship_course)) <= 45:
                    bearing_matches += 1
        signal_2 = bearing_matches >= threshold_pings
        signals["bearing_toward_sg"] = f"{bearing_matches}/{len(recent)}"

        # Signal 3: Cross-track drift drifting toward Singapore's side of
        #           the origin→destination great-circle route
        signal_3 = False
        try:
            sg_xtd  = cross_track_distance_nm(
                SINGAPORE["lat"], SINGAPORE["lon"],
                origin_lat, origin_lon, dest_lat, dest_lon,
            )
            sg_side = math.copysign(1.0, sg_xtd) if sg_xtd != 0.0 else 0.0

            xtd_values = [
                cross_track_distance_nm(
                    p['geo']['lat'], p['geo']['lon'],
                    origin_lat, origin_lon, dest_lat, dest_lon,
                )
                for p in recent
            ]
            same_side_count = sum(
                1 for x in xtd_values
                if sg_side != 0.0 and math.copysign(1.0, x) == sg_side
            )
            magnitudes = [abs(x) for x in xtd_values]
            mag_slope  = float(np.polyfit(range(len(magnitudes)), magnitudes, 1)[0]) \
                         if len(magnitudes) > 1 else 0.0

            signal_3 = (same_side_count >= threshold_pings) and (mag_slope > 0)
            signals["xtd_values_nm"]       = [round(x, 1) for x in xtd_values]
            signals["xtd_mag_slope"]       = round(mag_slope, 3)
            signals["sg_side_of_route_nm"] = round(sg_xtd, 1)
        except Exception as exc:
            signals["xtd_error"] = str(exc)

        score = int(signal_1) + int(signal_2) + int(signal_3)

        reason_parts = []
        if signal_1:
            reason_parts.append(
                f"[Non-SG] closing on SG (slope {signals['sg_slope']} nm/s) "
                f"while diverging from dest (slope {signals['dest_slope']} nm/s)"
            )
        if signal_2:
            reason_parts.append(
                f"[Non-SG] course toward SG on {signals['bearing_toward_sg']} pings"
            )
        if signal_3:
            reason_parts.append(
                f"[Non-SG] cross-track drift toward SG side "
                f"(slope {signals.get('xtd_mag_slope', '?')} nm/ping)"
            )

    # ══════════════════════════════════════════════════════════════════════
    # MODE B — SG-BOUND: detect unexpected diversion away from Singapore
    # ══════════════════════════════════════════════════════════════════════
    else:

        # Signal 1: Moving away from Singapore while not making progress
        #           toward any other plausible point (dest == SG so dest_slope
        #           mirrors sg_slope — we use SG distance alone here).
        #   Fires if distance to SG is consistently growing.
        sg_leaving = sg_slope > 0.001    # ship is moving away from SG
        signal_1   = sg_leaving
        signals["sg_leaving"] = sg_leaving

        # Signal 2: Course bearing persistently AWAY from Singapore
        #   The bearing is >135° off the direct Singapore heading on
        #   the majority of recent pings.
        bearing_away_count = 0
        for p in recent:
            bearing_to_sg = initial_bearing(p['geo']['lat'], p['geo']['lon'],
                                            SINGAPORE["lat"], SINGAPORE["lon"])
            ship_course   = p.get("course") or p.get("heading")
            if ship_course is not None:
                if bearing_diff(bearing_to_sg, float(ship_course)) > 45:
                    bearing_away_count += 1
        signal_2 = bearing_away_count >= threshold_pings
        signals["bearing_away_sg"] = f"{bearing_away_count}/{len(recent)}"

        # Signal 3: Cross-track deviation from the origin→Singapore route
        #           is large AND growing — ship is systematically leaving
        #           the expected corridor toward Singapore.
        signal_3 = False
        try:
            xtd_values = [
                cross_track_distance_nm(
                    p['geo']['lat'], p['geo']['lon'],
                    origin_lat, origin_lon,
                    SINGAPORE["lat"], SINGAPORE["lon"],   # route is origin→SG
                )
                for p in recent
            ]
            magnitudes = [abs(x) for x in xtd_values]
            mag_slope  = float(np.polyfit(range(len(magnitudes)), magnitudes, 1)[0]) \
                         if len(magnitudes) > 1 else 0.0
            # Also require the absolute deviation to be non-trivial (>20 nm)
            # so minor course adjustments don't fire this signal.
            max_deviation = max(magnitudes) if magnitudes else 0.0
            signal_3 = (mag_slope > 0) and (max_deviation > 20)
            signals["xtd_from_sg_route_nm"]  = [round(x, 1) for x in xtd_values]
            signals["xtd_mag_slope"]         = round(mag_slope, 3)
            signals["xtd_max_deviation_nm"]  = round(max_deviation, 1)
        except Exception as exc:
            signals["xtd_error"] = str(exc)

        score = int(signal_1) + int(signal_2) + int(signal_3)

        reason_parts = []
        if signal_1:
            reason_parts.append(
                f"[SG-bound] moving away from SG (slope {signals['sg_slope']} nm/s)"
            )
        if signal_2:
            reason_parts.append(
                f"[SG-bound] course away from SG on {signals['bearing_away_sg']} pings"
            )
        if signal_3:
            reason_parts.append(
                f"[SG-bound] cross-track deviation from SG route growing "
                f"(max {signals.get('xtd_max_deviation_nm', '?')} nm, "
                f"slope {signals.get('xtd_mag_slope', '?')} nm/ping)"
            )

    # ── Composite flag (shared by both modes) ─────────────────────────────
    flagged = (score >= DIVERSION_SCORE_THRESHOLD) and (chokepoint is None)

    if chokepoint:
        reason_parts.append(f"flag suppressed — near {chokepoint}")
    if not reason_parts:
        reason_parts.append("no diversion signals detected")

    return DiversionResult(
        ship_id=ship_id,
        ship_name=ship_name,
        score=score,
        flagged=flagged,
        signals=signals,
        near_chokepoint=chokepoint,
        latest_ping=latest,
        reason="; ".join(reason_parts),
        ais_gap_detected=ais_gap,
        ais_jump_info=jump_info,
        ais_jump_flagged=jump_flagged,
    )


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

def send_telegram_alert(result: DiversionResult) -> None:
    """
    Send a Telegram alert for a flagged ship.
    Runs in stub/log-only mode until TELEGRAM_BOT_TOKEN is set.
    """
    p    = result.latest_ping or {}
    lat = p.get("geo", {}).get("lat", 0.0)
    lon = p.get("geo", {}).get("lon", 0.0)
    spd  = p.get("speed", "?")
    # area = p.get("areaName", "Unknown area")
    ts   = p["receivedTime"] + " UTC" if p.get("receivedTime") else "?"

    text = (
        f"🚨 *Possible Singapore Diversion*\n\n"
        f"*Ship:* {result.ship_name}  (ID: `{result.ship_id}`)\n"
        f"*Score:* {result.score}/3 signals\n"
        f"*Reason:* {result.reason}\n"
    )

    if result.ais_gap_detected and result.ais_jump_info:
        j = result.ais_jump_info
        delta = j["sg_delta_nm"]
        direction_str = f"{'closer' if delta > 0 else 'farther'} by {abs(delta)} nm"
        text += (
            f"\n*AIS gap detected:* {j['gap_hours']}h dark\n"
            f"*Position jump:* {j['jump_nm']} nm\n"
            f"*SG distance:* {j['sg_dist_before_nm']} nm → "
            f"{j['sg_dist_after_nm']} nm ({direction_str})\n"
        )

    text += (
        f"\n*Latest position:* `{lat}, {lon}`\n"
        #f"*Area:* {area}\n"
        f"*Speed:* {spd} kn  |  *As of:* {ts}\n\n"
        f"[Track on MarineTraffic]"
        f"(https://www.marinetraffic.com/en/ais/details/ships/shipid:{result.ship_id})"
    )

    stub_mode = (not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN")
    if stub_mode:
        log.warning("[Telegram STUB] Alert for %s:\n%s", result.ship_name, text)
        return

    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = {
        "chat_id":                  TELEGRAM_CHANNEL_ID,
        "text":                     text,
        "parse_mode":               "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        log.info("Telegram alert sent for %s", result.ship_name)
    except requests.RequestException as exc:
        log.error("Telegram send failed for %s: %s", result.ship_name, exc)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def parse_pings(raw) -> list:
    """Parse a pings cell that contains a JSON-serialised list of dicts."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        log.warning("Could not parse pings cell: %s", str(raw)[:120])
        return []


def merge_positions(existing: list, fresh: list) -> list:
    """
    Merge fresh Kpler positions into the existing Coord_Trace list.
    Deduplicates by timestamp, returns sorted ascending.
    Fresh positions have keys like 'lat', 'lon', 'timestamp', 'speed',
    'course', 'heading' etc. — same shape the detection algo expects.
    """
    seen_ts = {p.get("receivedTime") for p in existing if p.get("receivedTime")}
    for p in fresh:
        if p.get("receivedTime") not in seen_ts:
            existing.append(p)
            seen_ts.add(p.get("receivedTime"))
    return sorted(existing, key=lambda p: p.get("receivedTime", 0))


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run() -> None:
    log.info("═══ Ship diversion check starting ═══")

    # ── Initialise handlers ───────────────────────────────────────────────
    gsheet  = GSheet_Handler(use_streamlit=False)          # reads SPREADSHEET_ID / SHEET_NAME from .env
    kpler   = KplerSession()            # loads saved tokens from kpler_tokens.json
    sheet   = gsheet.sheet

    ships   = get_all_ships(sheet)      # list of dicts keyed by COLUMNS
    if not ships:
        log.warning("Sheet has no data rows — nothing to process.")
        return

    log.info("Loaded %d ship(s) from sheet.", len(ships))
    flagged_count = 0

    # row_index is 1-based; row 1 is the header, data starts at row 2
    for row_idx, record in enumerate(ships, start=2):

        kpler_id   = record.get("KPLER_ID", "").strip()
        ship_name  = record.get("Name",     f"Ship@row{row_idx}")
        imo        = record.get("IMO",       "")
        departure  = record.get("Departure", "").strip()   # "YYYY-MM-DD"
        dest_port  = record.get("Original_Dest", "")
        dest_lat   = safe_float(record.get("Original_Dest_Lat"))
        dest_lon   = safe_float(record.get("Original_Dest_Long"))

        log.info("Row %d | %s (KPLER_ID=%s IMO=%s) → %s",
                 row_idx, ship_name, kpler_id, imo, dest_port)

        # ── Guard: need KPLER_ID and a departure date to fetch positions ──
        if not kpler_id or not departure:
            log.warning("  Skipping — missing KPLER_ID or Departure date.")
            continue

        # ── Step 1: Fetch fresh positions from Kpler ──────────────────────
        try:
            fresh_positions = kpler.get_positions(
                vessel_id   = int(kpler_id),
                departure_dt= departure,
            )
        except Exception as exc:
            log.error("  Kpler fetch failed for %s: %s", ship_name, exc)
            fresh_positions = None

        # # ── Step 2: Merge into existing Coord_Trace ───────────────────────
        # existing_pings = parse_pings(record.get("Coord_Trace", []))

        # if fresh_positions:
        #     merged_pings = merge_positions(existing_pings, fresh_positions)
        #     log.info("  Positions: %d existing + %d fresh → %d merged",
        #              len(existing_pings), len(fresh_positions), len(merged_pings))
        # else:
        #     merged_pings = existing_pings
        #     log.warning("  Using cached positions only (%d pings).", len(existing_pings))

        # # ── Step 3: Write merged positions back to sheet ──────────────────
        # if fresh_positions:
        #     record["Coord_Trace"] = merged_pings
        #     try:
        #         upsert_ship(sheet, row_idx, record)
        #         log.info("  Coord_Trace updated in sheet.")
        #     except Exception as exc:
        #         log.error("  Sheet write failed for %s: %s", ship_name, exc)

        # Step 2 and 3: used refreshed positions list due to changes caused by interference
        if fresh_positions:
            fresh_positions.sort(key=lambda x: x.get('receivedTime', ''))
            record["Coord_Trace"] = fresh_positions
            try:
                upsert_ship(sheet, row_idx, record)
                log.info("  Coord_Trace for %s updated in sheet.", ship_name)
            except Exception as exc:
                log.error("  Sheet write failed for %s: %s", ship_name, exc)

        # # ── Step 4: Run diversion detection ───────────────────────────────
        # if not merged_pings:
        #     log.warning("  No pings available — skipping detection.")
        #     continue

        first_ping = min(fresh_positions, key=lambda p: p.get("receivedTime", 0))
        origin_lat = first_ping.get("lat", 0.0)
        origin_lon = first_ping.get("lon", 0.0)

        result = detect_diversion(
            ship_name  = ship_name,
            ship_id    = str(kpler_id),
            dest_lat   = dest_lat,
            dest_lon   = dest_lon,
            origin_lat = origin_lat,
            origin_lon = origin_lon,
            pings      = fresh_positions,
        )

        log.info(
            "  score=%d/3  flagged=%s  ais_gap=%s  chokepoint=%s",
            result.score, result.flagged,
            result.ais_gap_detected, result.near_chokepoint or "—",
        )
        log.info("  reason: %s", result.reason)

        # ── Step 5: Alert if flagged ───────────────────────────────────────
        if result.flagged or result.ais_jump_flagged:
            flagged_count += 1
            send_telegram_alert(result)

    log.info(
        "═══ Done. %d/%d ship(s) flagged. ═══",
        flagged_count, len(ships),
    )


if __name__ == "__main__":
    run()


