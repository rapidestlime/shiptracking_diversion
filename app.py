import streamlit as st
import pandas as pd
import folium
import time
from streamlit_folium import st_folium
from gspread.exceptions import APIError
from datetime import datetime, timezone, timedelta

from gsheet_handler import GSheet_Handler, get_all_ships

st.set_page_config(
    page_title="Pertamina — Vessel Tracker",
    page_icon="🛢️",
    layout="wide",
    initial_sidebar_state="expanded",
)

PERTAMINA_LOGO = "https://upload.wikimedia.org/wikipedia/commons/e/e6/Pertamina_Logo.svg"
SGT = timezone(timedelta(hours=8))

st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=DM+Mono:wght@400;500&display=swap');

  html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
    background: #f0f4f8;
    color: #1e293b;
  }
  section[data-testid="stSidebar"] {
    background: #ffffff;
    border-right: 1px solid #e2e8f0;
  }
  .pertamina-header {
    display: flex; align-items: center; gap: 10px;
    padding-bottom: 10px;
    border-bottom: 2px solid #e2e8f0;
    margin-bottom: 14px;
  }
  .pertamina-header img { height: 36px; }
  .tracker-label {
    font-size: 0.68rem; font-weight: 600;
    color: #94a3b8; letter-spacing: 0.1em;
    text-transform: uppercase;
    font-family: 'DM Mono', monospace;
  }
  .stat-row { display:flex; gap:0.7rem; margin-bottom:1.1rem; }
  .stat-box {
    flex:1; background:#fff; border:1px solid #e2e8f0;
    border-radius:10px; padding:0.7rem 0.9rem; text-align:center;
    box-shadow: 0 1px 4px #0000000a;
  }
  .stat-val { font-size:1.5rem; font-weight:700; color:#0ea5e9; line-height:1; }
  .stat-val.red { color:#f43f5e; }
  .stat-lbl { font-size:0.62rem; color:#94a3b8; text-transform:uppercase;
              letter-spacing:0.1em; margin-top:2px; font-family:'DM Mono',monospace; }
  .page-title {
    font-size:1.5rem; font-weight:700; color:#0f172a;
    display:flex; align-items:center; gap:10px;
  }
  .page-title img { height:32px; }
  .fault-card {
    background:#fff7ed; border:1px solid #fed7aa;
    border-left:3px solid #f97316; border-radius:6px;
    padding:0.5rem 0.7rem; margin-bottom:0.4rem;
    font-size:0.72rem; font-family:'DM Mono',monospace; color:#7c2d12;
    line-height:1.5;
  }
  .fault-hdr {
    font-size:0.65rem; font-weight:700; color:#ea580c;
    text-transform:uppercase; letter-spacing:0.08em;
    margin-bottom:6px; font-family:'DM Mono',monospace;
  }
  .stMultiSelect [data-baseweb="tag"] { background-color:#0ea5e9!important; }
</style>
""", unsafe_allow_html=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

# def unix_to_sgt(ts) -> str:
#     """Convert Unix timestamp → SGT string."""
#     try:
#         dt = datetime.fromtimestamp(int(float(ts)), tz=SGT)
#         return dt.strftime("%Y-%m-%d %H:%M SGT")
#     except Exception:
#         return str(ts)


def validate_and_build_vessels(raw_records: list) -> tuple[list, list]:
    """
    Takes raw records from get_all_ships() and returns (valid_vessels, faulty_vessels).

    Each position dict in Coord_Trace has this structure:
      { "geo": {"lat": ..., "lon": ...}, "speed": ..., "course": ...,
        "draught": ..., "timestamp": ..., ... }
    """
    valid, faulty = [], []

    for row in raw_records:
        name = str(row.get("Name", "")).strip()
        if not name:
            faulty.append({"name": "Unknown", "error": "Missing vessel name"})
            continue

        # Coord_Trace is already parsed to a list by parse_row() in gsheet_handler
        raw_trace = row.get("Coord_Trace", [])
        if not isinstance(raw_trace, list) or len(raw_trace) == 0:
            faulty.append({"name": name, "error": "Coord_Trace is empty or unparseable"})
            continue

        clean, skipped = [], 0
        for pt in raw_trace:
            if not isinstance(pt, dict):
                skipped += 1; continue

            # lat/lon live inside pt["geo"]
            geo = pt.get("geo")
            if not isinstance(geo, dict):
                skipped += 1; continue

            try:
                lat = float(geo["lat"])
                lon = float(geo["lon"])
            except (KeyError, TypeError, ValueError):
                skipped += 1; continue

            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                skipped += 1; continue

            clean_pt = {
                "lat":           lat,
                "lon":           lon,
                "speed":         pt.get("speed"),
                "course":       pt.get("course"),
                "draught":       pt.get("draught"),
                "volume": pt.get("volume"),
                "timestamp_sgt": datetime.fromisoformat(pt.get('receivedTime')) + timedelta(hours=8) if pt.get('receivedTime') else "N/A",
                "timestamp_raw": pt.get("receivedTime", 0),
            }
            clean.append(clean_pt)

        if not clean:
            faulty.append({"name": name, "error": "All trace points invalid (missing or bad geo)"})
            continue

        # Sort ascending by raw timestamp (oldest → newest)
        clean.sort(key=lambda p: p["timestamp_raw"])

        if skipped:
            faulty.append({
                "name":  name,
                "error": f"{skipped} trace point(s) skipped (bad geo) — vessel still shown",
            })

        valid.append({
            "name":         name,
            "imo":          str(row.get("IMO", "N/A")).strip(),
            "kpler_id":     str(row.get("KPLER_ID", "N/A")).strip(),
            "departure":    str(row.get("Departure", "N/A")).strip(),
            "dest":         str(row.get("Original_Dest", "N/A")).strip(),
            "last_updated": str(row.get("Last_Updated", "")).strip(),
            "trace":        clean,
        })

    return valid, faulty


# ── Load data from GSheet ─────────────────────────────────────────────────────

# 1. Cache the AUTHENTICATION (The Client/Connection)
# We use cache_resource because this is a persistent object, not just data.
@st.cache_resource
def get_handler():
    try:
        # Try production secrets first, then local env
        return GSheet_Handler(use_streamlit=True)
    except (KeyError, FileNotFoundError):
        return GSheet_Handler(use_streamlit=False)
    except Exception as exc:
        st.error(f"❌ Google Sheets auth failed: {exc}")
        return None

# 2. Cache the DATA (The Records)
# Note the underscore in _handler: this tells Streamlit NOT to hash 
# the complex GSheet object, which prevents unnecessary cache resets.
@st.cache_data(ttl=1800)
def get_data(_handler) -> tuple[list, list]:
    if not _handler:
        return [], [{"name": "N/A", "error": "Auth Handler not initialized"}]
    
    # Retry logic for 503 errors (Service Unavailable)
    for attempt in range(3):
        try:
            raw_records = get_all_ships(_handler.sheet)
            return validate_and_build_vessels(raw_records)
        except APIError as e:
            # If it's a 503, wait and retry
            if e.response.status_code == 503 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            st.error(f"❌ Google Sheets API Error: {e}")
            return [], [{"name": "N/A", "error": str(e)}]
        except Exception as exc:
            st.error(f"❌ Could not read sheet: {exc}")
            return [], [{"name": "N/A", "error": str(exc)}]

# --- APP FLOW ---

with st.spinner("Connecting to Google Services..."):
    handler = get_handler()

with st.spinner("Loading vessel data from Google Sheets..."):
    all_vessels, faulty_vessels = get_data(handler)


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(
        f'<div class="pertamina-header">'
        f'<img src="{PERTAMINA_LOGO}" alt="Pertamina"/>'
        f'</div>'
        f'<div class="tracker-label">Vessel Tracking Dashboard</div>',
        unsafe_allow_html=True,
    )
    st.markdown("")

    vessel_names = [v["name"] for v in all_vessels]
    selected_names = st.multiselect(
        "Select Vessels",
        options=vessel_names,
        default=vessel_names,
        help="Toggle vessel traces on the map",
    ) if vessel_names else []

    st.markdown("---")

    if faulty_vessels:
        st.markdown(f'<div class="fault-hdr">⚠ Data Issues ({len(faulty_vessels)})</div>',
                    unsafe_allow_html=True)
        with st.expander(f"View {len(faulty_vessels)} issue(s)", expanded=False):
            for fv in faulty_vessels:
                st.markdown(
                    f'<div class="fault-card"><strong>{fv.get("name","Unknown")}</strong><br>'
                    f'{fv["error"]}</div>',
                    unsafe_allow_html=True,
                )
    else:
        st.success("✓ All rows passed validation")

    st.markdown("---")
    if st.button("🔄 Refresh Data"):
        get_data.clear()
        st.rerun()


# ── Main area ─────────────────────────────────────────────────────────────────
selected_vessels = [v for v in all_vessels if v["name"] in selected_names]

col_title, _ = st.columns([3, 1])
with col_title:
    st.markdown(
        f"""
        <div style="display: flex; align-items: center; gap: 15px;">
            <img src="{PERTAMINA_LOGO}" style="height: 40px;">
            <h1 style="margin: 0; font-size: 2rem;">Vessel Tracker</h1>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.markdown(
    f'<div class="stat-row">'
    f'<div class="stat-box"><div class="stat-val">{len(all_vessels)}</div>'
    f'<div class="stat-lbl">Valid Vessels</div></div>'
    f'<div class="stat-box"><div class="stat-val">{len(selected_vessels)}</div>'
    f'<div class="stat-lbl">Displayed</div></div>'
    f'<div class="stat-box"><div class="stat-val red">{len(faulty_vessels)}</div>'
    f'<div class="stat-lbl">Faulty Rows</div></div>'
    f'</div>',
    unsafe_allow_html=True,
)


# ── Color helpers ─────────────────────────────────────────────────────────────
VESSEL_COLORS = [
    "#0ea5e9", "#10b981", "#f43f5e", "#f97316",
    "#8b5cf6", "#eab308", "#ec4899", "#06b6d4",
    "#84cc16", "#6366f1",
]

def get_vessel_color(index: int, total: int) -> str:
    if index < len(VESSEL_COLORS):
        return VESSEL_COLORS[index]
    hue = (index * 137) % 360   # golden-angle spread for distinct hues
    return f"hsl({hue}, 75%, 48%)"


# ── Folium map ────────────────────────────────────────────────────────────────
m = folium.Map(
    location=[10, 60],
    zoom_start=3,
    tiles="CartoDB positron",
    prefer_canvas=True,
)

for i, vessel in enumerate(selected_vessels):
    color = get_vessel_color(i, len(selected_vessels))
    trace = vessel["trace"]
    if not trace:
        continue

    coords = [[pt["lat"], pt["lon"]] for pt in trace]

    # Polyline
    folium.PolyLine(
        locations=coords,
        color=color,
        weight=3,
        opacity=0.85,
        tooltip=folium.Tooltip(
            f"<b style='color:{color}'>{vessel['name']}</b><br>"
            f"IMO: {vessel['imo']} · KPLER_ID: {vessel['kpler_id']}<br>"
            f"{len(trace)} waypoints",
            sticky=True,
        ),
    ).add_to(m)

    # Waypoint circle markers — skip last point (replaced by triangle)
    last_idx = len(trace) - 1
    step = max(1, len(trace) // 60)
    for idx in range(0, len(trace), step):
        if idx == last_idx:
            continue
        pt = trace[idx]
        draught_str = f"{pt['draught']} m" if pt.get("draught") is not None else "N/A"
        volume_str = f"{pt['volume']}" if pt.get("volume") is not None else "N/A"
        popup_html = (
            f"<div style='font-family:monospace;min-width:210px;font-size:12px'>"
            f"<b style='color:{color};font-size:13px'>{vessel['name']}</b>"
            f"<hr style='margin:4px 0'>"
            f"<b>Time (SGT):</b> {pt['timestamp_sgt']}<br>"
            f"<b>Lat:</b> {pt['lat']:.5f} &nbsp; <b>Lon:</b> {pt['lon']:.5f}<br>"
            f"<b>Draught:</b> {draught_str}<br>"
            f"<b>Volume (Bbls):</b> {volume_str}"
            f"</div>"
        )
        folium.CircleMarker(
            location=[pt["lat"], pt["lon"]],
            radius=4,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=0.55,
            weight=1,
            popup=folium.Popup(popup_html, max_width=260),
            tooltip=folium.Tooltip(
                f"<b style='color:{color}'>{vessel['name']}</b><br>"
                f"{pt['timestamp_sgt']}<br>"
                f"Lat {pt['lat']:.4f} · Lon {pt['lon']:.4f}<br>"
                f"Draught: {draught_str}",
                sticky=True,
            ),
        ).add_to(m)

    # ── Latest position: triangle rotated to course ──────────────────────────
    last        = trace[-1]
    course     = last.get("course") or 0
    draught_str = f"{last['draught']} m" if last.get("draught") is not None else "N/A"

    arrow_html = (
        f"<svg width='24' height='24' viewBox='0 0 24 24'"
        f" style='transform:rotate({course}deg);display:block;overflow:visible;'>"
        f"  <polygon points='12,2 2,22 22,22' fill='{color}'/>"
        f"</svg>"
    )

    latest_popup = (
        f"<div style='font-family:monospace;min-width:210px;font-size:12px'>"
        f"<b style='color:{color};font-size:13px'>{vessel['name']} — CURRENT</b>"
        f"<hr style='margin:4px 0'>"
        f"<b>Time (SGT):</b> {last['timestamp_sgt']}<br>"
        f"<b>Lat:</b> {last['lat']:.5f} &nbsp; <b>Lon:</b> {last['lon']:.5f}<br>"
        f"<b>Draught:</b> {draught_str}<br>"
        f"<b>course:</b> {course}°<br>"
        f"<b>Destination:</b> {vessel['dest']}<br>"
        f"<b>Departure:</b> {vessel['departure']}"
        f"</div>"
    )

    folium.Marker(
        location=[last["lat"], last["lon"]],
        icon=folium.DivIcon(html=arrow_html, icon_size=(24, 24), icon_anchor=(12, 12)),
        popup=folium.Popup(latest_popup, max_width=270),
        tooltip=f"{vessel['name']} — latest position",
    ).add_to(m)

st_folium(m, use_container_width=True, height=600, returned_objects=[])

# ── Details table ─────────────────────────────────────────────────────────────
if selected_vessels:
    with st.expander("📋 Vessel Details", expanded=False):
        rows = []
        for v in selected_vessels:
            last = v["trace"][-1]
            rows.append({
                "Vessel":          v["name"],
                "IMO":             v["imo"],
                "KPLER_ID":        v["kpler_id"],
                "Departure":       v["departure"],
                "Waypoints":       len(v["trace"]),
                "Last Time (SGT)": last["timestamp_sgt"],
                "Last Lat":        round(last["lat"], 5),
                "Last Lon":        round(last["lon"], 5),
                "Draught (m)":     last.get("draught", "N/A"),
                "course (°)":     last.get("course", "N/A"),
                "Volume (m3)": last.get("volume", "N/A"),
                "Destination":     v["dest"],
                "Last Updated":    v["last_updated"],
            })
        st.dataframe(all_vessels, width="stretch")